from __future__ import annotations

import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
MODEL = os.environ["MODEL_ID"]
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))


def detect_repo_root(cwd: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        root = Path(result.stdout.strip())
        return root if result.returncode == 0 and root.exists() else None
    except Exception:
        return None


REPO_ROOT = detect_repo_root(WORKDIR) or WORKDIR

# Directories
TEAM_DIR = REPO_ROOT / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
REQUESTS_DIR = TEAM_DIR / "requests"
TASKS_DIR = REPO_ROOT / ".tasks"
CLAIM_EVENTS_PATH = TASKS_DIR / "claim_events.jsonl"
RUNTIME_DIR = REPO_ROOT / ".runtime-tasks"
SCHEDULED_TASKS_FILE = REPO_ROOT / ".claude" / "scheduled_tasks.json"
CRON_LOCK_FILE = REPO_ROOT / ".claude" / "cron.lock"
SKILLS_DIR = REPO_ROOT / "skills"
TRANSCRIPT_DIR = REPO_ROOT / ".transcripts"
TOOL_RESULTS_DIR = REPO_ROOT / ".task_outputs" / "tool-results"
MEMORY_DIR = REPO_ROOT / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
HOOK_CONFIG_FILE = REPO_ROOT / ".hooks.json"
TRUST_MARKER = REPO_ROOT / ".claude" / ".claude_trusted"

# Tuning knobs
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60
STALL_THRESHOLD_S = 45
AUTO_EXPIRY_DAYS = 7
JITTER_MINUTES = [0, 30]
JITTER_OFFSET_MAX = 4
PLAN_REMINDER_INTERVAL = 3
CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30000
PREVIEW_CHARS = 2000
MEMORY_TYPES = ("user", "feedback", "project", "reference")
MAX_INDEX_LINES = 200
DYNAMIC_BOUNDARY = "=== DYNAMIC_BOUNDARY ==="
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")
HOOK_TIMEOUT = 30
PERMISSION_MODES = ("default", "plan", "auto")
MAX_RECOVERY_ATTEMPTS = 3
BACKOFF_BASE_DELAY = 1.0
BACKOFF_MAX_DELAY = 30.0
TOKEN_THRESHOLD = 50000
CONTINUATION_MESSAGE = (
    "Output limit hit. Continue directly from where you stopped -- "
    "no recap, no repetition. Pick up mid-sentence if needed."
)

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval",
    "plan_approval_response",
}

READ_ONLY_TOOLS = {
    "read_file",
    "skill_list",
    "load_skill",
    "list_teammates",
    "read_inbox",
    "task_list",
    "task_get",
    "check_background",
    "cron_list",
    "worktree_list",
    "worktree_status",
    "worktree_events",
    "shutdown_response",
}

WRITE_TOOLS = {
    "bash",
    "write_file",
    "edit_file",
    "todo",
    "skill_reload",
    "task",
    "compact",
    "spawn_teammate",
    "send_message",
    "broadcast",
    "shutdown_request",
    "plan_approval",
    "task_create",
    "task_update",
    "claim_task",
    "background_run",
    "cron_create",
    "cron_delete",
    "worktree_create",
    "worktree_enter",
    "worktree_run",
    "worktree_closeout",
    "worktree_keep",
    "worktree_remove",
    "save_memory",
    "memory_dream",
}

SYSTEM = (
    f"You are a team lead at {WORKDIR}. Coordinate named teammates, protocol "
    "handshakes, durable task graphs, background execution, scheduled work, "
    "isolated git worktrees, optional MCP tools, subagents, skills, session "
    "todos, and context compaction."
)
