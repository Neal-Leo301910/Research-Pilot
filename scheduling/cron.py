from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread

from config import (
    SCHEDULED_TASKS_FILE,
    CRON_LOCK_FILE,
    JITTER_MINUTES,
    JITTER_OFFSET_MAX,
    AUTO_EXPIRY_DAYS,
)


def cron_matches(expr: str, dt: datetime) -> bool:
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    values = [dt.minute, dt.hour, dt.day, dt.month, (dt.weekday() + 1) % 7]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    return all(_field_matches(field, value, lo, hi) for field, value, (lo, hi) in zip(fields, values, ranges))


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    if field == "*":
        return True
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
        if part == "*":
            if (value - lo) % step == 0:
                return True
        elif "-" in part:
            start, end = part.split("-", 1)
            start, end = int(start), int(end)
            if start <= value <= end and (value - start) % step == 0:
                return True
        elif int(part) == value:
            return True
    return False


class CronLock:
    """PID-file lock to prevent multiple sessions from firing durable cron tasks."""

    def __init__(self, lock_path: Path = CRON_LOCK_FILE):
        self._lock_path = lock_path

    def acquire(self) -> bool:
        if self._lock_path.exists():
            try:
                stored_pid = int(self._lock_path.read_text(encoding="utf-8").strip())
                os.kill(stored_pid, 0)
                return False
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                pass
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.write_text(str(os.getpid()), encoding="utf-8")
        return True

    def release(self):
        try:
            if self._lock_path.exists():
                stored_pid = int(self._lock_path.read_text(encoding="utf-8").strip())
                if stored_pid == os.getpid():
                    self._lock_path.unlink()
        except (ValueError, OSError):
            pass


class CronScheduler:
    """Cron-style future prompt scheduler with optional disk persistence."""

    def __init__(self):
        self.tasks: list[dict] = []
        self.queue: Queue[str] = Queue()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._last_check_minute = -1
        self._lock = CronLock()
        self._has_lock = False

    def start(self):
        self._load_durable()
        self._has_lock = self._lock.acquire()
        if not self._has_lock:
            print("[Cron] Lock held by another process; this session will not fire schedules.")
            return
        self._thread = Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        if self.tasks:
            print(f"[Cron] Loaded {len(self.tasks)} scheduled tasks")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._has_lock:
            self._lock.release()

    def create(self, cron_expr: str, prompt: str, recurring: bool = True, durable: bool = False) -> str:
        task_id = str(uuid.uuid4())[:8]
        task = {
            "id": task_id,
            "cron": cron_expr,
            "prompt": prompt,
            "recurring": recurring,
            "durable": durable,
            "createdAt": time.time(),
        }
        if recurring:
            task["jitter_offset"] = self._compute_jitter(cron_expr)
        self.tasks.append(task)
        if durable:
            self._save_durable()
        mode = "recurring" if recurring else "one-shot"
        store = "durable" if durable else "session-only"
        return f"Created scheduled task {task_id} ({mode}, {store}): cron={cron_expr}"

    def delete(self, task_id: str) -> str:
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t["id"] != task_id]
        if len(self.tasks) < before:
            self._save_durable()
            return f"Deleted scheduled task {task_id}"
        return f"Scheduled task {task_id} not found"

    def list_tasks(self) -> str:
        if not self.tasks:
            return "No scheduled tasks."
        lines = []
        for task in self.tasks:
            mode = "recurring" if task["recurring"] else "one-shot"
            store = "durable" if task["durable"] else "session"
            age_hours = (time.time() - task["createdAt"]) / 3600
            lines.append(f"  {task['id']}  {task['cron']}  [{mode}/{store}] ({age_hours:.1f}h old): {task['prompt'][:60]}")
        return "\n".join(lines)

    def drain_notifications(self) -> list[str]:
        notifications = []
        while True:
            try:
                notifications.append(self.queue.get_nowait())
            except Empty:
                break
        return notifications

    def enqueue_test(self) -> str:
        self.queue.put("[Scheduled task test-0000]: This is a test notification.")
        return "Test cron notification enqueued. It will be injected before the next model call."

    def detect_missed_tasks(self) -> list[dict]:
        now = datetime.now()
        missed = []
        for task in self.tasks:
            last_fired = task.get("last_fired")
            if last_fired is None:
                continue
            last_dt = datetime.fromtimestamp(last_fired)
            check = last_dt + timedelta(minutes=1)
            cap = min(now, last_dt + timedelta(hours=24))
            while check <= cap:
                if cron_matches(task["cron"], check):
                    missed.append({"id": task["id"], "cron": task["cron"], "prompt": task["prompt"], "missed_at": check.isoformat()})
                    break
                check += timedelta(minutes=1)
        return missed

    def _compute_jitter(self, cron_expr: str) -> int:
        fields = cron_expr.strip().split()
        if not fields:
            return 0
        try:
            minute_val = int(fields[0])
            if minute_val in JITTER_MINUTES:
                return (hash(cron_expr) % JITTER_OFFSET_MAX) + 1
        except ValueError:
            pass
        return 0

    def _check_loop(self):
        while not self._stop_event.is_set():
            now = datetime.now()
            current_minute = now.hour * 60 + now.minute
            if current_minute != self._last_check_minute:
                self._last_check_minute = current_minute
                self._check_tasks(now)
            self._stop_event.wait(timeout=1)

    def _check_tasks(self, now: datetime):
        expired = []
        fired_oneshots = []
        for task in self.tasks:
            age_days = (time.time() - task["createdAt"]) / 86400
            if task["recurring"] and age_days > AUTO_EXPIRY_DAYS:
                expired.append(task["id"])
                continue
            check_time = now - timedelta(minutes=task.get("jitter_offset", 0))
            if cron_matches(task["cron"], check_time):
                self.queue.put(f"[Scheduled task {task['id']}]: {task['prompt']}")
                task["last_fired"] = time.time()
                print(f"[Cron] Fired: {task['id']}")
                if not task["recurring"]:
                    fired_oneshots.append(task["id"])
        if expired or fired_oneshots:
            remove_ids = set(expired) | set(fired_oneshots)
            self.tasks = [t for t in self.tasks if t["id"] not in remove_ids]
            self._save_durable()
        elif any(t.get("durable") and t.get("last_fired") for t in self.tasks):
            self._save_durable()

    def _load_durable(self):
        if not SCHEDULED_TASKS_FILE.exists():
            return
        try:
            data = json.loads(SCHEDULED_TASKS_FILE.read_text(encoding="utf-8"))
            self.tasks = [t for t in data if t.get("durable")]
        except Exception as e:
            print(f"[Cron] Error loading tasks: {e}")

    def _save_durable(self):
        durable = [t for t in self.tasks if t.get("durable")]
        SCHEDULED_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEDULED_TASKS_FILE.write_text(json.dumps(durable, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
