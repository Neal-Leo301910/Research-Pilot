from __future__ import annotations

import json
import time
from pathlib import Path

from config import REPO_ROOT


class TaskManager:
    def __init__(self, tasks_dir: Path = REPO_ROOT / ".tasks"):
        self.dir = tasks_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        ids = []
        for f in self.dir.glob("task_*.json"):
            try:
                ids.append(int(f.stem.split("_", 1)[1]))
            except Exception:
                pass
        return max(ids) if ids else 0

    def _path(self, task_id: int) -> Path:
        return self.dir / f"task_{task_id}.json"

    def _load(self, task_id: int) -> dict:
        path = self._path(task_id)
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, task: dict):
        self._path(task["id"]).write_text(
            json.dumps(task, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def create(self, subject: str, description: str = "", claim_role: str = "") -> str:
        task = {
            "id": self._next_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "owner": "",
            "claim_role": claim_role,
            "worktree": "",
            "worktree_state": "unbound",
            "last_worktree": "",
            "closeout": None,
            "blockedBy": [],
            "blocks": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def exists(self, task_id: int) -> bool:
        return self._path(task_id).exists()

    def update(
        self,
        task_id: int,
        status: str | None = None,
        owner: str | None = None,
        add_blocked_by: list[int] | None = None,
        add_blocks: list[int] | None = None,
    ) -> str:
        task = self._load(task_id)
        if status:
            if status not in ("pending", "in_progress", "completed", "deleted"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
            if status == "completed":
                self._clear_dependency(task_id)
        if owner is not None:
            task["owner"] = owner
        if add_blocked_by:
            task["blockedBy"] = sorted(set(task.get("blockedBy", []) + add_blocked_by))
            for blocker_id in add_blocked_by:
                try:
                    blocker = self._load(blocker_id)
                except ValueError:
                    continue
                blocker["blocks"] = sorted(set(blocker.get("blocks", []) + [task_id]))
                blocker["updated_at"] = time.time()
                self._save(blocker)
        if add_blocks:
            task["blocks"] = sorted(set(task.get("blocks", []) + add_blocks))
            for blocked_id in add_blocks:
                try:
                    blocked = self._load(blocked_id)
                except ValueError:
                    continue
                blocked["blockedBy"] = sorted(set(blocked.get("blockedBy", []) + [task_id]))
                blocked["updated_at"] = time.time()
                self._save(blocked)
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def _clear_dependency(self, completed_id: int):
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text(encoding="utf-8"))
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                task["updated_at"] = time.time()
                self._save(task)

    def bind_worktree(self, task_id: int, worktree: str, owner: str = "") -> str:
        task = self._load(task_id)
        task["worktree"] = worktree
        task["last_worktree"] = worktree
        task["worktree_state"] = "active"
        if owner:
            task["owner"] = owner
        if task["status"] == "pending":
            task["status"] = "in_progress"
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def record_closeout(self, task_id: int, action: str, reason: str = "", keep_binding: bool = False) -> str:
        task = self._load(task_id)
        task["closeout"] = {"action": action, "reason": reason, "at": time.time()}
        task["worktree_state"] = action
        if not keep_binding:
            task["worktree"] = ""
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def list_all(self) -> str:
        tasks = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(self.dir.glob("task_*.json"))]
        if not tasks:
            return "No tasks."
        lines = []
        for task in tasks:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "deleted": "[-]"}.get(task["status"], "[?]")
            owner = f" owner={task['owner']}" if task.get("owner") else ""
            wt = f" wt={task['worktree']}" if task.get("worktree") else ""
            blocked = f" blockedBy={task['blockedBy']}" if task.get("blockedBy") else ""
            blocks = f" blocks={task['blocks']}" if task.get("blocks") else ""
            lines.append(f"{marker} #{task['id']}: {task['subject']}{owner}{wt}{blocked}{blocks}")
        return "\n".join(lines)
