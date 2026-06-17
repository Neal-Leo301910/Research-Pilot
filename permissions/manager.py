from __future__ import annotations

import json
from fnmatch import fnmatch
from pathlib import Path

from config import (
    PERMISSION_MODES,
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
    REPO_ROOT,
    TRUST_MARKER,
)
from tools.bash import bash_validator

DEFAULT_PERMISSION_RULES = [
    {"tool": "bash",      "content": "rm -rf /", "behavior": "deny"},
    {"tool": "bash",      "content": "sudo *",   "behavior": "deny"},
    {"tool": "read_file", "path": "*",            "behavior": "allow"},
    {"tool": "skill_list","path": "*",            "behavior": "allow"},
    {"tool": "load_skill","path": "*",            "behavior": "allow"},
]


def is_workspace_trusted(workspace: Path | None = None) -> bool:
    ws = workspace or REPO_ROOT
    return (ws / ".claude" / ".claude_trusted").exists()


class PermissionManager:
    """Permission pipeline: bash validation → deny rules → mode → allow rules → ask."""

    def __init__(self, mode: str = "default", rules: list | None = None):
        if mode not in PERMISSION_MODES:
            mode = "default"
        self.mode = mode
        self.rules = rules or list(DEFAULT_PERMISSION_RULES)
        self.consecutive_denials = 0
        self.max_consecutive_denials = 3

    def normalize(self, tool_name: str, tool_input: dict) -> dict:
        if tool_name.startswith("mcp__"):
            _, server_name, actual_tool = tool_name.split("__", 2)
            source = "mcp"
        else:
            server_name = None
            actual_tool = tool_name
            source = "native"

        if (
            actual_tool in READ_ONLY_TOOLS
            or actual_tool.startswith(("read", "list", "get", "show", "search", "query", "inspect"))
        ):
            risk = "read"
        elif actual_tool in {"bash", "worktree_run", "background_run"}:
            command = tool_input.get("command", "")
            failures = bash_validator.validate(command)
            risk = "high" if any(n in {"sudo", "rm_rf"} for n, _ in failures) else "write"
        elif actual_tool.lower().startswith(("delete", "remove", "drop", "shutdown")):
            risk = "high"
        else:
            risk = "write"

        return {"source": source, "server": server_name, "tool": actual_tool, "risk": risk}

    def check(self, tool_name: str, tool_input: dict) -> dict:
        intent = self.normalize(tool_name, tool_input)

        # bash security validator runs first
        if tool_name == "bash":
            command = tool_input.get("command", "")
            failures = bash_validator.validate(command)
            if failures:
                severe = [f for f in failures if f[0] in {"sudo", "rm_rf"}]
                desc = bash_validator.describe_failures(command)
                if severe:
                    return {"behavior": "deny", "reason": f"Bash validator: {desc}", "intent": intent}
                return {"behavior": "ask", "reason": f"Bash validator flagged: {desc}", "intent": intent}

        # explicit deny rules
        for rule in self.rules:
            if rule.get("behavior") == "deny" and self._matches(rule, tool_name, tool_input):
                self.consecutive_denials += 1
                return {"behavior": "deny", "reason": f"Blocked by deny rule: {rule}", "intent": intent}

        # plan mode: block all writes
        if self.mode == "plan":
            if tool_name in WRITE_TOOLS or intent["risk"] in {"write", "high"}:
                return {"behavior": "deny", "reason": "Plan mode: write operations are blocked", "intent": intent}
            return {"behavior": "allow", "reason": "Plan mode: read-only allowed", "intent": intent}

        # auto mode: pass reads immediately
        if self.mode == "auto" and intent["risk"] == "read":
            return {"behavior": "allow", "reason": "Auto mode: read-only tool auto-approved", "intent": intent}

        # explicit allow rules
        for rule in self.rules:
            if rule.get("behavior") == "allow" and self._matches(rule, tool_name, tool_input):
                self.consecutive_denials = 0
                return {"behavior": "allow", "reason": f"Matched allow rule: {rule}", "intent": intent}

        # auto mode: pass non-high-risk writes
        if self.mode == "auto" and intent["risk"] != "high":
            return {"behavior": "allow", "reason": "Auto mode: non-high-risk tool auto-approved", "intent": intent}

        return {"behavior": "ask", "reason": f"No rule matched for {tool_name}, asking user", "intent": intent}

    def ask_user(self, intent: dict, tool_input: dict) -> bool:
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        source = (
            f"{intent['source']}:{intent['server']}/{intent['tool']}"
            if intent.get("server")
            else f"{intent['source']}:{intent['tool']}"
        )
        print(f"\n  [Permission] {source} risk={intent['risk']}: {preview}")
        try:
            answer = input("  Allow? (y/n/always): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer == "always":
            self.rules.append({"tool": intent["tool"], "path": "*", "behavior": "allow"})
            self.consecutive_denials = 0
            return True
        if answer in ("y", "yes"):
            self.consecutive_denials = 0
            return True
        self.consecutive_denials += 1
        if self.consecutive_denials >= self.max_consecutive_denials:
            print(f"  [{self.consecutive_denials} consecutive denials — consider switching to plan mode]")
        return False

    def _matches(self, rule: dict, tool_name: str, tool_input: dict) -> bool:
        rule_tool = rule.get("tool")
        actual_tool = tool_name.split("__", 2)[-1] if tool_name.startswith("mcp__") else tool_name
        if rule_tool and rule_tool != "*" and rule_tool not in {tool_name, actual_tool}:
            return False
        if "path" in rule and rule["path"] != "*":
            path = tool_input.get("path", "")
            if not fnmatch(path, rule["path"]):
                return False
        if "content" in rule:
            command = tool_input.get("command", "")
            if not fnmatch(command, rule["content"]):
                return False
        return True
