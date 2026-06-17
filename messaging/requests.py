from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


class RequestStore:
    """Durable protocol request records under .team/requests."""

    def __init__(self, base_dir: Path):
        import threading
        self.dir = base_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, request_id: str) -> Path:
        return self.dir / f"{request_id}.json"

    def create(self, record: dict) -> dict:
        with self._lock:
            self._path(record["request_id"]).write_text(
                json.dumps(record, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        return record

    def get(self, request_id: str) -> dict | None:
        path = self._path(request_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def update(self, request_id: str, **changes) -> dict | None:
        with self._lock:
            record = self.get(request_id)
            if not record:
                return None
            record.update(changes)
            record["updated_at"] = time.time()
            self._path(request_id).write_text(
                json.dumps(record, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        return record


# ── Protocol helpers ──────────────────────────────────────────────────────────

def handle_shutdown_request(teammate: str, bus, request_store: RequestStore) -> str:
    req_id = str(uuid.uuid4())[:8]
    request_store.create({
        "request_id": req_id,
        "kind": "shutdown",
        "from": "lead",
        "to": teammate,
        "status": "pending",
        "created_at": time.time(),
        "updated_at": time.time(),
    })
    bus.send("lead", teammate, "Please shut down gracefully.", "shutdown_request", {"request_id": req_id})
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(
    request_id: str,
    approve: bool,
    feedback: str,
    bus,
    request_store: RequestStore,
) -> str:
    req = request_store.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    request_store.update(
        request_id,
        status="approved" if approve else "rejected",
        reviewed_by="lead",
        resolved_at=time.time(),
        feedback=feedback,
    )
    bus.send(
        "lead",
        req["from"],
        feedback,
        "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"Plan {'approved' if approve else 'rejected'} for '{req['from']}'"


def check_request_status(request_id: str, request_store: RequestStore) -> str:
    return json.dumps(
        request_store.get(request_id) or {"error": "not found"},
        indent=2,
        ensure_ascii=False,
    )
