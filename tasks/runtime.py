"""
tasks/runtime.py

运行时任务层 —— 与工作图任务（WorkGraphTask）互补的执行层。

关键区别：
  WorkGraphTask  (tasks/manager.py)   durable，面向目标与依赖，生命周期长
  RuntimeTask    (本文件)              runtime，面向执行与输出，生命周期短

关系：
  一个 WorkGraphTask 可以派生一个或多个 RuntimeTask。
  通过 work_graph_task_id 字段关联回工作图。

RuntimeTaskType 类型族：
  local_bash          本地 shell 命令
  local_agent         本地子 agent（subagent）
  remote_agent        远程 agent
  in_process_teammate 线程内 teammate
  monitor             监控/轮询任务
  workflow            多步骤编排任务
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from config import RUNTIME_DIR
from tools.files import display_path

# ── RuntimeTaskType ───────────────────────────────────────────────────────────

RuntimeTaskType = Literal[
    "local_bash",
    "local_agent",
    "remote_agent",
    "in_process_teammate",
    "monitor",
    "workflow",
]

RUNTIME_TASK_TYPES: tuple[str, ...] = (
    "local_bash",
    "local_agent",
    "remote_agent",
    "in_process_teammate",
    "monitor",
    "workflow",
)

# ── RuntimeTaskStatus ─────────────────────────────────────────────────────────

RuntimeTaskStatus = Literal["queued", "running", "completed", "error", "timeout", "cancelled"]


# ── RuntimeTaskState ──────────────────────────────────────────────────────────

@dataclass
class RuntimeTaskState:
    """
    单个运行时任务的完整状态。

    字段重点：
      type            它是什么执行单元
      status          它现在在运行态还是终态
      output_file     它的产出在哪
      notified        结果有没有回通知系统
      work_graph_task_id  关联回哪个工作图任务（可选）
    """
    id: str
    type: RuntimeTaskType
    status: RuntimeTaskStatus
    description: str

    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    output_file: str = ""
    result_preview: str = ""
    notified: bool = False

    # 关联回工作图任务（可选）
    work_graph_task_id: int | None = None

    # 类型专属元数据
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id":                  self.id,
            "type":                self.type,
            "status":              self.status,
            "description":         self.description,
            "start_time":          self.start_time,
            "end_time":            self.end_time,
            "output_file":         self.output_file,
            "result_preview":      self.result_preview,
            "notified":            self.notified,
            "work_graph_task_id":  self.work_graph_task_id,
            "meta":                self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RuntimeTaskState":
        return cls(
            id=d["id"],
            type=d["type"],
            status=d["status"],
            description=d.get("description", ""),
            start_time=d.get("start_time", 0.0),
            end_time=d.get("end_time"),
            output_file=d.get("output_file", ""),
            result_preview=d.get("result_preview", ""),
            notified=d.get("notified", False),
            work_graph_task_id=d.get("work_graph_task_id"),
            meta=d.get("meta", {}),
        )

    def is_terminal(self) -> bool:
        return self.status in {"completed", "error", "timeout", "cancelled"}


# ── RuntimeTaskManager ────────────────────────────────────────────────────────

class RuntimeTaskManager:
    """
    管理所有运行时任务的生命周期。

    职责：
      - 创建各类型 RuntimeTask
      - 追踪状态转移
      - 产出完成通知
      - 支持关联回 WorkGraphTask
      - 持久化到 runtime_dir（可选，默认开启）
    """

    def __init__(self, runtime_dir: Path = RUNTIME_DIR, persist: bool = True):
        self.dir = runtime_dir
        self.persist = persist
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tasks: dict[str, RuntimeTaskState] = {}
        self._notification_queue: list[dict] = []
        self._lock = threading.Lock()
        if persist:
            self._load_existing()

    # ── 工厂方法 ──────────────────────────────────────────────────────────────

    def create(
        self,
        task_type: RuntimeTaskType,
        description: str,
        work_graph_task_id: int | None = None,
        meta: dict | None = None,
        output_file: str = "",
    ) -> RuntimeTaskState:
        """创建并注册一个新的运行时任务（初始 status=queued）。"""
        task_id = str(uuid.uuid4())[:8]
        task = RuntimeTaskState(
            id=task_id,
            type=task_type,
            status="queued",
            description=description,
            work_graph_task_id=work_graph_task_id,
            meta=meta or {},
            output_file=output_file,
        )
        with self._lock:
            self.tasks[task_id] = task
            if self.persist:
                self._save(task)
        return task

    def spawn_bash_task(
        self,
        command: str,
        work_graph_task_id: int | None = None,
    ) -> RuntimeTaskState:
        """
        创建 local_bash 类型运行时任务并立即标记为 running。

        实际执行由 BackgroundManager 负责；
        这里只创建状态记录，供全局 RuntimeTaskManager 追踪。
        """
        output_file = str(self.dir / f"{uuid.uuid4().hex[:8]}.log")
        task = self.create(
            task_type="local_bash",
            description=command[:120],
            work_graph_task_id=work_graph_task_id,
            meta={"command": command},
            output_file=output_file,
        )
        self.mark_running(task.id)
        return task

    def spawn_agent_task(
        self,
        prompt: str,
        work_graph_task_id: int | None = None,
        remote: bool = False,
    ) -> RuntimeTaskState:
        task_type: RuntimeTaskType = "remote_agent" if remote else "local_agent"
        task = self.create(
            task_type=task_type,
            description=prompt[:120],
            work_graph_task_id=work_graph_task_id,
            meta={"prompt": prompt},
        )
        self.mark_running(task.id)
        return task

    def spawn_teammate_task(
        self,
        name: str,
        role: str,
        work_graph_task_id: int | None = None,
    ) -> RuntimeTaskState:
        task = self.create(
            task_type="in_process_teammate",
            description=f"{name} ({role})",
            work_graph_task_id=work_graph_task_id,
            meta={"name": name, "role": role},
        )
        self.mark_running(task.id)
        return task

    # ── 状态转移 ──────────────────────────────────────────────────────────────

    def mark_running(self, task_id: str) -> None:
        self._transition(task_id, "running")

    def mark_completed(
        self,
        task_id: str,
        result_preview: str = "",
        output_file: str = "",
    ) -> None:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task.status = "completed"
            task.end_time = time.time()
            task.result_preview = result_preview
            if output_file:
                task.output_file = output_file
            if self.persist:
                self._save(task)
            self._enqueue_notification(task)

    def mark_error(self, task_id: str, reason: str = "") -> None:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task.status = "error"
            task.end_time = time.time()
            task.result_preview = reason[:200]
            if self.persist:
                self._save(task)
            self._enqueue_notification(task)

    def mark_timeout(self, task_id: str) -> None:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task.status = "timeout"
            task.end_time = time.time()
            if self.persist:
                self._save(task)
            self._enqueue_notification(task)

    def cancel(self, task_id: str) -> str:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return f"Error: Unknown runtime task {task_id}"
            if task.is_terminal():
                return f"Task {task_id} already in terminal state: {task.status}"
            task.status = "cancelled"
            task.end_time = time.time()
            if self.persist:
                self._save(task)
        return f"Cancelled runtime task {task_id}"

    # ── 查询 ──────────────────────────────────────────────────────────────────

    def get(self, task_id: str) -> RuntimeTaskState | None:
        return self.tasks.get(task_id)

    def list_all(self, task_type: RuntimeTaskType | None = None) -> str:
        tasks = list(self.tasks.values())
        if task_type:
            tasks = [t for t in tasks if t.type == task_type]
        if not tasks:
            return "No runtime tasks."
        lines = []
        for t in sorted(tasks, key=lambda x: x.start_time):
            wg = f" wg={t.work_graph_task_id}" if t.work_graph_task_id is not None else ""
            lines.append(
                f"[{t.status:<10}] {t.id}  {t.type:<22} {t.description[:50]}{wg}"
            )
        return "\n".join(lines)

    def list_by_work_graph_task(self, work_graph_task_id: int) -> list[RuntimeTaskState]:
        """返回某个工作图任务派生的所有运行时任务。"""
        return [t for t in self.tasks.values() if t.work_graph_task_id == work_graph_task_id]

    def detect_stalled(self, threshold_s: float = 45.0) -> list[str]:
        now = time.time()
        return [
            tid for tid, t in self.tasks.items()
            if t.status == "running" and (now - t.start_time) > threshold_s
        ]

    # ── 通知 ──────────────────────────────────────────────────────────────────

    def drain_notifications(self) -> list[dict]:
        with self._lock:
            result = list(self._notification_queue)
            self._notification_queue.clear()
            for n in result:
                task = self.tasks.get(n["task_id"])
                if task:
                    task.notified = True
        return result

    # ── 内部 ──────────────────────────────────────────────────────────────────

    def _transition(self, task_id: str, status: RuntimeTaskStatus) -> None:
        with self._lock:
            task = self.tasks.get(task_id)
            if task:
                task.status = status
                if self.persist:
                    self._save(task)

    def _enqueue_notification(self, task: RuntimeTaskState) -> None:
        """调用方已持有 self._lock 时调用。"""
        self._notification_queue.append({
            "task_id":             task.id,
            "type":                task.type,
            "status":              task.status,
            "description":         task.description[:80],
            "result_preview":      task.result_preview[:200],
            "output_file":         task.output_file,
            "work_graph_task_id":  task.work_graph_task_id,
        })

    def _save(self, task: RuntimeTaskState) -> None:
        path = self.dir / f"rt_{task.id}.json"
        path.write_text(json.dumps(task.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_existing(self) -> None:
        for path in self.dir.glob("rt_*.json"):
            try:
                task = RuntimeTaskState.from_dict(json.loads(path.read_text(encoding="utf-8")))
                self.tasks[task.id] = task
            except Exception:
                pass
