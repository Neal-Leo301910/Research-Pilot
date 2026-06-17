"""
tasks/background.py

BackgroundManager —— local_bash 类型运行时任务的执行器。

与 RuntimeTaskManager 的分工：
  BackgroundManager   负责实际 subprocess 执行、线程管理、输出写盘
  RuntimeTaskManager  负责全局状态追踪、通知队列、工作图关联

每次 run() 调用时：
  1. 在 RuntimeTaskManager 创建 RuntimeTaskState（type=local_bash）
  2. 启动后台线程执行命令
  3. 完成后通过 RuntimeTaskManager 更新状态并入通知队列
"""
from __future__ import annotations

import subprocess
import threading
import time
import uuid
from pathlib import Path

from config import WORKDIR, RUNTIME_DIR, STALL_THRESHOLD_S
from tools.files import display_path

_DANGEROUS_TOKENS = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]


class BackgroundManager:
    """
    本地 bash 命令的后台执行器。

    与 RuntimeTaskManager 配合使用：
      - BackgroundManager 管执行
      - RuntimeTaskManager 管状态 & 通知
    """

    def __init__(
        self,
        runtime_dir: Path = RUNTIME_DIR,
        runtime_task_mgr=None,   # RuntimeTaskManager | None
    ):
        self.dir = runtime_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._runtime_task_mgr = runtime_task_mgr
        self._lock = threading.Lock()
        # 保留轻量本地 map 供 check() 查询（不依赖 RuntimeTaskManager）
        self._local: dict[str, dict] = {}

    def set_runtime_task_mgr(self, mgr) -> None:
        """延迟注入，避免循环依赖。"""
        self._runtime_task_mgr = mgr

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    def run(
        self,
        command: str,
        work_graph_task_id: int | None = None,
    ) -> str:
        """
        启动后台 bash 任务。

        如果注入了 RuntimeTaskManager，会同时创建 RuntimeTaskState
        并通过它管理状态与通知。
        """
        if any(token in command for token in _DANGEROUS_TOKENS):
            return "Error: Dangerous command blocked"

        task_id = str(uuid.uuid4())[:8]
        output_path = self.dir / f"{task_id}.log"

        # 在 RuntimeTaskManager 创建 local_bash 运行时任务
        if self._runtime_task_mgr:
            rt_task = self._runtime_task_mgr.spawn_bash_task(
                command=command,
                work_graph_task_id=work_graph_task_id,
            )
            rt_id = rt_task.id
        else:
            rt_id = task_id

        # 本地轻量记录（供 check() 独立工作）
        record = {
            "id":                 task_id,
            "rt_id":              rt_id,
            "status":             "running",
            "command":            command,
            "started_at":         time.time(),
            "output_file":        display_path(output_path),
            "work_graph_task_id": work_graph_task_id,
        }
        with self._lock:
            self._local[task_id] = record

        thread = threading.Thread(
            target=self._execute,
            args=(task_id, rt_id, command, output_path),
            daemon=True,
        )
        thread.start()

        wg_info = f" (wg={work_graph_task_id})" if work_graph_task_id is not None else ""
        return (
            f"Background task {task_id} started: {command[:80]}"
            f" (output_file={display_path(output_path)}){wg_info}"
        )

    def check(self, task_id: str | None = None) -> str:
        """查询本地 bash 任务状态（不依赖 RuntimeTaskManager）。"""
        import json
        if task_id:
            rec = self._local.get(task_id)
            if not rec:
                return f"Error: Unknown background task {task_id}"
            return json.dumps({k: v for k, v in rec.items() if k != "result_full"}, indent=2, ensure_ascii=False)
        lines = []
        for tid, rec in sorted(self._local.items()):
            lines.append(
                f"{tid}: [{rec['status']}] {rec['command'][:60]}"
                f" -> {rec.get('result_preview', '(running)')}"
            )
        return "\n".join(lines) if lines else "No background tasks."

    def drain_notifications(self) -> list[dict]:
        """
        如果有 RuntimeTaskManager，通知由它统一管理；
        否则从本地队列返回（向后兼容）。
        """
        if self._runtime_task_mgr:
            return []   # 上层直接调 runtime_task_mgr.drain_notifications()
        with self._lock:
            notes = list(self._local_notifications)
            self._local_notifications.clear()
        return notes

    def detect_stalled(self, threshold_s: float = STALL_THRESHOLD_S) -> list[str]:
        now = time.time()
        return [
            tid for tid, rec in self._local.items()
            if rec["status"] == "running" and (now - rec["started_at"]) > threshold_s
        ]

    # ── 内部执行 ──────────────────────────────────────────────────────────────

    def _execute(self, task_id: str, rt_id: str, command: str, output_path: Path) -> None:
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = (result.stdout + result.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "Error: Timeout (300s)"
            status = "timeout"
        except Exception as e:
            output = f"Error: {e}"
            status = "error"

        final_output = output or "(no output)"
        preview = " ".join(final_output.split())[:500]
        output_path.write_text(final_output, encoding="utf-8")

        with self._lock:
            rec = self._local.get(task_id)
            if rec:
                rec["status"] = status
                rec["finished_at"] = time.time()
                rec["result_preview"] = preview

        # 通过 RuntimeTaskManager 更新状态 & 入通知队列
        if self._runtime_task_mgr:
            if status == "completed":
                self._runtime_task_mgr.mark_completed(
                    rt_id,
                    result_preview=preview,
                    output_file=display_path(output_path),
                )
            elif status == "timeout":
                self._runtime_task_mgr.mark_timeout(rt_id)
            else:
                self._runtime_task_mgr.mark_error(rt_id, reason=preview)

    # 向后兼容：没有 RuntimeTaskManager 时的本地通知队列
    @property
    def _local_notifications(self):
        if not hasattr(self, "_local_notif_list"):
            self._local_notif_list = []
        return self._local_notif_list
