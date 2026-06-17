from __future__ import annotations

from config import VALID_MSG_TYPES

BASE_FILE_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]},
    },
    {
        "name": "write_file",
        "description": "Write content to file.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in file.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]},
    },
]

SKILL_TOOL = {
    "name": "load_skill",
    "description": "Load the full body of a named skill into the current context.",
    "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
}

NATIVE_TOOLS = [
    *BASE_FILE_TOOLS,
    {
        "name": "todo",
        "description": "Rewrite the current short session plan. Keep at most one item in_progress.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                            "activeForm": {"type": "string"},
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["items"],
        },
    },
    {"name": "load_skill", "description": "Load the full body of a named skill into the current context.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "skill_list", "description": "List available local skills from the skills directory.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "skill_reload", "description": "Reload local skills from disk.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "save_memory", "description": "Save a persistent cross-session memory. Do not store secrets or temporary task state.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"]}, "content": {"type": "string"}}, "required": ["name", "description", "type", "content"]}},
    {"name": "memory_list", "description": "List persistent memories currently loaded.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "memory_dream", "description": "Run the gated memory consolidation demo.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "task", "description": "Spawn a one-shot subagent with fresh context. It shares the filesystem and returns only a summary.", "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "description": {"type": "string", "description": "Short task description"}}, "required": ["prompt"]}},
    {"name": "compact", "description": "Summarize earlier conversation so work can continue in a smaller context.", "input_schema": {"type": "object", "properties": {"focus": {"type": "string"}}}},
    {"name": "spawn_teammate", "description": "Spawn an autonomous persistent teammate.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate.", "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": sorted(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead inbox.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.", "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "shutdown_request", "description": "Request a teammate to shut down gracefully.", "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "shutdown_response", "description": "Check protocol request status by request_id.", "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "plan_approval", "description": "Approve or reject a teammate plan.", "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    {"name": "task_create", "description": "Create a task on the shared board.", "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}, "claim_role": {"type": "string"}}, "required": ["subject"]}},
    {"name": "task_list", "description": "List tasks.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "task_get", "description": "Get task details.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
    {"name": "task_update", "description": "Update task status, owner, or dependency graph.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]}, "owner": {"type": "string"}, "addBlockedBy": {"type": "array", "items": {"type": "integer"}}, "addBlocks": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
    {"name": "claim_task", "description": "Claim a task from the board by ID.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
    {"name": "background_run", "description": "Run a shell command in a background thread and return a runtime task ID immediately.", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "check_background", "description": "Check one background task by task_id, or omit task_id to list all runtime background tasks.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
    {"name": "cron_create", "description": "Schedule a recurring or one-shot prompt with a 5-field cron expression.", "input_schema": {"type": "object", "properties": {"cron": {"type": "string", "description": "5-field cron expression: min hour dom month dow"}, "prompt": {"type": "string", "description": "Prompt to inject when the schedule fires"}, "recurring": {"type": "boolean", "description": "true repeats, false fires once then deletes"}, "durable": {"type": "boolean", "description": "true persists to .claude/scheduled_tasks.json"}}, "required": ["cron", "prompt"]}},
    {"name": "cron_delete", "description": "Delete a scheduled task by ID.", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "cron_list", "description": "List scheduled tasks.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "worktree_create", "description": "Create a git worktree and optionally bind it to a task.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "task_id": {"type": "integer"}, "base_ref": {"type": "string"}}, "required": ["name"]}},
    {"name": "worktree_list", "description": "List tracked worktrees.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "worktree_enter", "description": "Enter or reopen a worktree lane.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "worktree_status", "description": "Show git status for one worktree.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "worktree_run", "description": "Run a shell command in a named worktree.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "command": {"type": "string"}}, "required": ["name", "command"]}},
    {"name": "worktree_closeout", "description": "Keep or remove a worktree lane.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "action": {"type": "string", "enum": ["keep", "remove"]}, "reason": {"type": "string"}, "force": {"type": "boolean"}, "complete_task": {"type": "boolean"}}, "required": ["name", "action"]}},
    {"name": "worktree_keep", "description": "Mark a worktree as kept.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "reason": {"type": "string"}, "complete_task": {"type": "boolean"}}, "required": ["name"]}},
    {"name": "worktree_remove", "description": "Remove a worktree.", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "force": {"type": "boolean"}, "complete_task": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["name"]}},
    {"name": "worktree_events", "description": "List recent lifecycle events.", "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
]


def build_tool_pool(mcp_router=None) -> list:
    all_tools = list(NATIVE_TOOLS)
    if mcp_router is not None:
        native_names = {tool["name"] for tool in all_tools}
        for tool in mcp_router.get_all_tools():
            if tool["name"] not in native_names:
                all_tools.append(tool)
    return all_tools
