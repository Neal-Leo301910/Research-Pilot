from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from config import (
    MODEL, client, WORKDIR, TEAM_DIR,
    VALID_MSG_TYPES, POLL_INTERVAL, IDLE_TIMEOUT,
)
from core.messages import normalize_messages
from tools.bash import run_bash
from tools.files import run_read, run_write, run_edit
from tasks.claiming import claim_task, scan_unclaimed_tasks


def make_identity_block(name: str, role: str, team_name: str) -> dict:
    return {
        "role": "user",
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }


def ensure_identity_context(messages: list[dict], name: str, role: str, team_name: str):
    if messages and "<identity>" in str(messages[0].get("content", "")):
        return
    messages.insert(0, make_identity_block(name, role, team_name))
    messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})


def teammate_tools() -> list[dict]:
    return [
        {
            "name": "bash",
            "description": "Run a shell command.",
            "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        },
        {
            "name": "read_file",
            "description": "Read file contents.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]},
        },
        {
            "name": "write_file",
            "description": "Write content to file.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
        },
        {
            "name": "edit_file",
            "description": "Replace exact text in file.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]},
        },
        {
            "name": "send_message",
            "description": "Send message to a teammate.",
            "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": sorted(VALID_MSG_TYPES)}}, "required": ["to", "content"]},
        },
        {"name": "read_inbox", "description": "Read and drain your inbox.", "input_schema": {"type": "object", "properties": {}}},
        {
            "name": "shutdown_response",
            "description": "Respond to a shutdown request.",
            "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]},
        },
        {"name": "plan_approval", "description": "Submit a plan for lead approval.", "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
        {"name": "idle", "description": "Signal that you have no more work. Enters idle polling phase.", "input_schema": {"type": "object", "properties": {}}},
        {"name": "claim_task", "description": "Claim a task from the task board by ID.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        {"name": "task_list", "description": "List tasks.", "input_schema": {"type": "object", "properties": {}}},
        {"name": "task_get", "description": "Get task details by ID.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        {
            "name": "task_update",
            "description": "Update task status, owner, or dependency graph.",
            "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]}, "owner": {"type": "string"}, "addBlockedBy": {"type": "array", "items": {"type": "integer"}}, "addBlocks": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]},
        },
    ]


def teammate_exec(sender: str, tool_name: str, args: dict, bus, request_store, tasks, team) -> str:
    import uuid
    if tool_name == "bash":
        return run_bash(args["command"])
    if tool_name == "read_file":
        return run_read(args["path"], args.get("limit"))
    if tool_name == "write_file":
        return run_write(args["path"], args["content"])
    if tool_name == "edit_file":
        return run_edit(args["path"], args["old_text"], args["new_text"])
    if tool_name == "send_message":
        return bus.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
    if tool_name == "read_inbox":
        return json.dumps(bus.read_inbox(sender), indent=2, ensure_ascii=False)
    if tool_name == "shutdown_response":
        req_id = args["request_id"]
        approve = args["approve"]
        updated = request_store.update(
            req_id,
            status="approved" if approve else "rejected",
            resolved_by=sender,
            resolved_at=time.time(),
            response={"approve": approve, "reason": args.get("reason", "")},
        )
        if not updated:
            return f"Error: Unknown shutdown request {req_id}"
        bus.send(sender, "lead", args.get("reason", ""), "shutdown_response", {"request_id": req_id, "approve": approve})
        return f"Shutdown {'approved' if approve else 'rejected'}"
    if tool_name == "plan_approval":
        plan_text = args.get("plan", "")
        req_id = str(uuid.uuid4())[:8]
        request_store.create({
            "request_id": req_id,
            "kind": "plan_approval",
            "from": sender,
            "to": "lead",
            "status": "pending",
            "plan": plan_text,
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        bus.send(sender, "lead", plan_text, "plan_approval", {"request_id": req_id, "plan": plan_text})
        return f"Plan submitted (request_id={req_id}). Waiting for approval."
    if tool_name == "claim_task":
        return claim_task(args["task_id"], sender, role=team.role_for(sender), source="manual")
    if tool_name == "task_list":
        return tasks.list_all()
    if tool_name == "task_get":
        return tasks.get(args["task_id"])
    if tool_name == "task_update":
        return tasks.update(args["task_id"], args.get("status"), args.get("owner"), args.get("addBlockedBy"), args.get("addBlocks"))
    return f"Unknown tool: {tool_name}"


class TeammateManager:
    def __init__(self, team_dir: Path, bus, request_store, tasks):
        self.dir = team_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._bus = bus
        self._request_store = request_store
        self._tasks = tasks

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8")

    def _find_member(self, name: str) -> dict | None:
        for member in self.config["members"]:
            if member["name"] == name:
                return member
        return None

    def _set_status(self, name: str, status: str):
        with self._lock:
            member = self._find_member(name)
            if member:
                member["status"] = status
                self._save_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        with self._lock:
            member = self._find_member(name)
            if member:
                if member["status"] not in ("idle", "shutdown"):
                    return f"Error: '{name}' is currently {member['status']}"
                member["status"] = "working"
                member["role"] = role
            else:
                member = {"name": name, "role": role, "status": "working"}
                self.config["members"].append(member)
            self._save_config()
        thread = threading.Thread(target=self._loop, args=(name, role, prompt), daemon=True)
        self.threads[name] = thread
        thread.start()
        return f"Spawned '{name}' (role: {role})"

    def _loop(self, name: str, role: str, prompt: str):
        team_name = self.config["team_name"]
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
            "Submit plans via plan_approval before major work. Respond to shutdown_request. "
            "Use idle when you have no more work; you can auto-claim matching tasks."
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tools = teammate_tools()
        while True:
            for _ in range(50):
                inbox = self._bus.read_inbox(name)
                for msg in inbox:
                    messages.append({"role": "user", "content": json.dumps(msg, ensure_ascii=False)})
                try:
                    response = client.messages.create(
                        model=MODEL,
                        system=sys_prompt,
                        messages=messages,
                        tools=tools,
                        max_tokens=8000,
                    )
                except Exception:
                    self._set_status(name, "idle")
                    return
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    break
                results = []
                idle_requested = False
                should_exit = False
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    if block.name == "idle":
                        idle_requested = True
                        output = "Entering idle phase. Will poll for new tasks."
                    else:
                        output = teammate_exec(name, block.name, block.input, self._bus, self._request_store, self._tasks, self)
                    if block.name == "shutdown_response" and block.input.get("approve"):
                        should_exit = True
                    print(f"  [{name}] {block.name}: {str(output)[:120]}")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                messages.append({"role": "user", "content": results})
                if should_exit:
                    self._set_status(name, "shutdown")
                    return
                if idle_requested:
                    break

            self._set_status(name, "idle")
            resume = False
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)
            for _ in range(polls):
                time.sleep(POLL_INTERVAL)
                inbox = self._bus.read_inbox(name)
                if inbox:
                    ensure_identity_context(messages, name, role, team_name)
                    for msg in inbox:
                        messages.append({"role": "user", "content": json.dumps(msg, ensure_ascii=False)})
                    resume = True
                    break
                unclaimed = scan_unclaimed_tasks(role)
                if unclaimed:
                    task = unclaimed[0]
                    result = claim_task(task["id"], name, role=role, source="auto")
                    if result.startswith("Error:"):
                        continue
                    ensure_identity_context(messages, name, role, team_name)
                    messages.append({
                        "role": "user",
                        "content": (
                            f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                            f"{task.get('description', '')}</auto-claimed>"
                        ),
                    })
                    messages.append({"role": "assistant", "content": f"{result}. Working on it."})
                    resume = True
                    break
            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def list_all(self) -> str:
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for member in self.config["members"]:
            lines.append(f"  {member['name']} ({member['role']}): {member['status']}")
        return "\n".join(lines)

    def member_names(self) -> list[str]:
        return [m["name"] for m in self.config["members"]]

    def role_for(self, name: str) -> str | None:
        member = self._find_member(name)
        return member.get("role") if member else None
