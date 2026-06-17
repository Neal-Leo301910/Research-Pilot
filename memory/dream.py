from __future__ import annotations

import os
import time
from pathlib import Path

from config import MEMORY_DIR


class DreamConsolidator:
    """Visible four-phase memory consolidation gate demo."""

    COOLDOWN_SECONDS = 86400
    SCAN_THROTTLE_SECONDS = 600
    MIN_SESSION_COUNT = 5
    LOCK_STALE_SECONDS = 3600
    PHASES = [
        "Orient: scan MEMORY.md index for structure and categories",
        "Gather: read individual memory files for full content",
        "Consolidate: merge related memories, remove stale entries",
        "Prune: enforce 200-line limit on MEMORY.md index",
    ]

    def __init__(self, memory_dir: Path = MEMORY_DIR):
        self.memory_dir = memory_dir
        self.lock_file = self.memory_dir / ".dream_lock"
        self.enabled = True
        self.mode = "default"
        self.last_consolidation_time = 0.0
        self.last_scan_time = 0.0
        self.session_count = 0

    def should_consolidate(self) -> tuple[bool, str]:
        now = time.time()
        if not self.enabled:
            return False, "Gate 1: consolidation is disabled"
        if not self.memory_dir.exists():
            return False, "Gate 2: memory directory does not exist"
        memory_files = [f for f in self.memory_dir.glob("*.md") if f.name != "MEMORY.md"]
        if not memory_files:
            return False, "Gate 2: no memory files found"
        if self.mode == "plan":
            return False, "Gate 3: plan mode does not allow consolidation"
        if now - self.last_consolidation_time < self.COOLDOWN_SECONDS:
            return False, "Gate 4: cooldown active"
        if now - self.last_scan_time < self.SCAN_THROTTLE_SECONDS:
            return False, "Gate 5: scan throttle active"
        if self.session_count < self.MIN_SESSION_COUNT:
            return False, f"Gate 6: only {self.session_count} sessions, need {self.MIN_SESSION_COUNT}"
        if not self._acquire_lock():
            return False, "Gate 7: lock held by another process"
        return True, "All 7 gates passed"

    def consolidate(self) -> str:
        can_run, reason = self.should_consolidate()
        if not can_run:
            return f"[Dream] Cannot consolidate: {reason}"
        self.last_scan_time = time.time()
        completed = []
        for phase in self.PHASES:
            completed.append(phase)
        self.last_consolidation_time = time.time()
        self._release_lock()
        return "[Dream] Consolidation complete:\n" + "\n".join(f"- {phase}" for phase in completed)

    def _acquire_lock(self) -> bool:
        if self.lock_file.exists():
            try:
                pid_str, timestamp_str = self.lock_file.read_text(encoding="utf-8").strip().split(":", 1)
                pid = int(pid_str)
                lock_time = float(timestamp_str)
                if time.time() - lock_time > self.LOCK_STALE_SECONDS:
                    self.lock_file.unlink()
                else:
                    os.kill(pid, 0)
                    return False
            except (ValueError, OSError, ProcessLookupError):
                self.lock_file.unlink(missing_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.lock_file.write_text(f"{os.getpid()}:{time.time()}", encoding="utf-8")
        return True

    def _release_lock(self):
        try:
            if self.lock_file.exists():
                pid_str = self.lock_file.read_text(encoding="utf-8").strip().split(":", 1)[0]
                if int(pid_str) == os.getpid():
                    self.lock_file.unlink()
        except (ValueError, OSError):
            pass
