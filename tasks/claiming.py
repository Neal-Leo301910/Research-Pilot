from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from config import TASKS_DIR, CLAIM_EVENTS_PATH

_claim_lock = threading.Lock()


def append_claim_event(payload: dict):
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    with CLAIM_EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def task_allows_role(task: dict, role: str | None) -> bool:
    required_role = task.get("claim_role") or task.get("required_role") or ""
    return not required_role or bool(role and role == required_role)


def is_claimable_task(task: dict, role: str | None = None) -> bool:
    return (
        task.get("status") == "pending"
        and not task.get("owner")
        and not task.get("blockedBy")
        and task_allows_role(task, role)
    )


def scan_unclaimed_tasks(role: str | None = None) -> list[dict]:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text(encoding="utf-8"))
        if is_claimable_task(task, role):
            unclaimed.append(task)
    return unclaimed


def claim_task(task_id: int, owner: str, role: str | None = None, source: str = "manual") -> str:
    with _claim_lock:
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"Error: Task {task_id} not found"
        task = json.loads(path.read_text(encoding="utf-8"))
        if not is_claimable_task(task, role):
            return f"Error: Task {task_id} is not claimable for role={role or '(any)'}"
        task["owner"] = owner
        task["status"] = "in_progress"
        task["claimed_at"] = time.time()
        task["claim_source"] = source
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
    append_claim_event({
        "event": "task.claimed",
        "task_id": task_id,
        "owner": owner,
        "role": role,
        "source": source,
        "ts": time.time(),
    })
    return f"Claimed task #{task_id} for {owner} via {source}"
