from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from config import VALID_MSG_TYPES


class MessageBus:
    """Append-only JSONL inbox per teammate."""

    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def send(
        self,
        sender: str,
        to: str,
        content: str,
        msg_type: str = "message",
        extra: dict | None = None,
    ) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {sorted(VALID_MSG_TYPES)}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with self._lock:
            with inbox_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list[dict]:
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        with self._lock:
            lines = inbox_path.read_text(encoding="utf-8").splitlines()
            inbox_path.write_text("", encoding="utf-8")
        messages = []
        for line in lines:
            if line.strip():
                messages.append(json.loads(line))
        return messages

    def broadcast(self, sender: str, content: str, teammates: list[str]) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"
