"""
tools/handlers.py

两种 handler 签名：
  旧式（向后兼容）: handler(**tool_input)          → str
  新式（推荐）:     handler(tool_input, ctx)        → str | ToolResultEnvelope

native_handlers() 返回新式签名的 dispatch map。
handle_tool_call() / route_tool() 对外统一接口。
"""
from __future__ import annotations

import json

from tools.bash import run_bash
from tools.files import run_read, run_write, run_edit
from tools.context import ToolUseContext, ToolResultEnvelope


# ── native_handlers：新式签名，handler(tool_input, ctx) ───────────────────────

def native_handlers(
    todo_mgr,
    skill_registry,
    memory_mgr,
    dream_consolidator,
    tasks,
    team,
    bus,
    request_store,
    bg,
    scheduler,
    worktrees,
    events,
    permission_gate,
    claim_task_fn,
    handle_shutdown_request_fn,
    handle_plan_review_fn,
    check_request_status_fn,
    run_subagent_fn,
) -> dict:
    """
    返回 {tool_name: handler} map。

    每个 handler 签名: (tool_input: dict, ctx: ToolUseContext) -> str | ToolResultEnvelope
    ctx 让 handler 在需要时访问权限、消息历史、通知队列等平台能力。
    """

    def _bash(inp: dict, ctx: ToolUseContext):
        return run_bash(inp["command"])

    def _read_file(inp: dict, ctx: ToolUseContext):
        return run_read(inp["path"], inp.get("limit"))

    def _write_file(inp: dict, ctx: ToolUseContext):
        return run_write(inp["path"], inp["content"])

    def _edit_file(inp: dict, ctx: ToolUseContext):
        return run_edit(inp["path"], inp["old_text"], inp["new_text"])

    def _todo(inp: dict, ctx: ToolUseContext):
        return todo_mgr.update(inp["items"])

    def _load_skill(inp: dict, ctx: ToolUseContext):
        return skill_registry.load_full_text(inp["name"])

    def _skill_list(inp: dict, ctx: ToolUseContext):
        return skill_registry.describe_available()

    def _skill_reload(inp: dict, ctx: ToolUseContext):
        return skill_registry.reload()

    def _save_memory(inp: dict, ctx: ToolUseContext):
        return memory_mgr.save_memory(inp["name"], inp["description"], inp["type"], inp["content"])

    def _memory_list(inp: dict, ctx: ToolUseContext):
        return memory_mgr.list_memories()

    def _memory_dream(inp: dict, ctx: ToolUseContext):
        return dream_consolidator.consolidate()

    def _task(inp: dict, ctx: ToolUseContext):
        return run_subagent_fn(inp["prompt"], inp.get("description", "subtask"))

    def _compact(inp: dict, ctx: ToolUseContext):
        return "Compacting conversation..."

    def _spawn_teammate(inp: dict, ctx: ToolUseContext):
        return team.spawn(inp["name"], inp["role"], inp["prompt"])

    def _list_teammates(inp: dict, ctx: ToolUseContext):
        return team.list_all()

    def _send_message(inp: dict, ctx: ToolUseContext):
        return bus.send("lead", inp["to"], inp["content"], inp.get("msg_type", "message"))

    def _read_inbox(inp: dict, ctx: ToolUseContext):
        return json.dumps(bus.read_inbox("lead"), indent=2, ensure_ascii=False)

    def _broadcast(inp: dict, ctx: ToolUseContext):
        return bus.broadcast("lead", inp["content"], team.member_names())

    def _shutdown_request(inp: dict, ctx: ToolUseContext):
        return handle_shutdown_request_fn(inp["teammate"])

    def _shutdown_response(inp: dict, ctx: ToolUseContext):
        return check_request_status_fn(inp.get("request_id", ""))

    def _plan_approval(inp: dict, ctx: ToolUseContext):
        return handle_plan_review_fn(inp["request_id"], inp["approve"], inp.get("feedback", ""))

    def _task_create(inp: dict, ctx: ToolUseContext):
        return tasks.create(inp["subject"], inp.get("description", ""), inp.get("claim_role", ""))

    def _task_list(inp: dict, ctx: ToolUseContext):
        return tasks.list_all()

    def _task_get(inp: dict, ctx: ToolUseContext):
        return tasks.get(inp["task_id"])

    def _task_update(inp: dict, ctx: ToolUseContext):
        return tasks.update(
            inp["task_id"], inp.get("status"), inp.get("owner"),
            inp.get("addBlockedBy"), inp.get("addBlocks"),
        )

    def _claim_task(inp: dict, ctx: ToolUseContext):
        return claim_task_fn(inp["task_id"], "lead", source="manual")

    def _background_run(inp: dict, ctx: ToolUseContext):
        result = bg.run(inp["command"])
        # 后台任务启动通知写入 ctx，主循环通过 drain_notifications 消费
        ctx.push_notification("background_started", {"command": inp["command"][:80]})
        return result

    def _check_background(inp: dict, ctx: ToolUseContext):
        return bg.check(inp.get("task_id"))

    def _cron_create(inp: dict, ctx: ToolUseContext):
        return scheduler.create(inp["cron"], inp["prompt"], inp.get("recurring", True), inp.get("durable", False))

    def _cron_delete(inp: dict, ctx: ToolUseContext):
        return scheduler.delete(inp["id"])

    def _cron_list(inp: dict, ctx: ToolUseContext):
        return scheduler.list_tasks()

    def _worktree_create(inp: dict, ctx: ToolUseContext):
        return worktrees.create(inp["name"], inp.get("task_id"), inp.get("base_ref", "HEAD"))

    def _worktree_list(inp: dict, ctx: ToolUseContext):
        return worktrees.list_all()

    def _worktree_enter(inp: dict, ctx: ToolUseContext):
        return worktrees.enter(inp["name"])

    def _worktree_status(inp: dict, ctx: ToolUseContext):
        return worktrees.status(inp["name"])

    def _worktree_run(inp: dict, ctx: ToolUseContext):
        return worktrees.run(inp["name"], inp["command"])

    def _worktree_closeout(inp: dict, ctx: ToolUseContext):
        return worktrees.closeout(
            inp["name"], inp["action"], inp.get("reason", ""),
            inp.get("force", False), inp.get("complete_task", False),
        )

    def _worktree_keep(inp: dict, ctx: ToolUseContext):
        return worktrees.keep(inp["name"], inp.get("reason", ""), inp.get("complete_task", False))

    def _worktree_remove(inp: dict, ctx: ToolUseContext):
        return worktrees.remove(
            inp["name"], inp.get("force", False), inp.get("complete_task", False), inp.get("reason", ""),
        )

    def _worktree_events(inp: dict, ctx: ToolUseContext):
        return events.list_recent(inp.get("limit", 20))

    return {
        "bash":              _bash,
        "read_file":         _read_file,
        "write_file":        _write_file,
        "edit_file":         _edit_file,
        "todo":              _todo,
        "load_skill":        _load_skill,
        "skill_list":        _skill_list,
        "skill_reload":      _skill_reload,
        "save_memory":       _save_memory,
        "memory_list":       _memory_list,
        "memory_dream":      _memory_dream,
        "task":              _task,
        "compact":           _compact,
        "spawn_teammate":    _spawn_teammate,
        "list_teammates":    _list_teammates,
        "send_message":      _send_message,
        "read_inbox":        _read_inbox,
        "broadcast":         _broadcast,
        "shutdown_request":  _shutdown_request,
        "shutdown_response": _shutdown_response,
        "plan_approval":     _plan_approval,
        "task_create":       _task_create,
        "task_list":         _task_list,
        "task_get":          _task_get,
        "task_update":       _task_update,
        "claim_task":        _claim_task,
        "background_run":    _background_run,
        "check_background":  _check_background,
        "cron_create":       _cron_create,
        "cron_delete":       _cron_delete,
        "cron_list":         _cron_list,
        "worktree_create":   _worktree_create,
        "worktree_list":     _worktree_list,
        "worktree_enter":    _worktree_enter,
        "worktree_status":   _worktree_status,
        "worktree_run":      _worktree_run,
        "worktree_closeout": _worktree_closeout,
        "worktree_keep":     _worktree_keep,
        "worktree_remove":   _worktree_remove,
        "worktree_events":   _worktree_events,
    }


# ── route_tool：按能力来源路由，全部经过 ToolUseContext ───────────────────────

def route_tool(tool_name: str, tool_input: dict, ctx: ToolUseContext) -> ToolResultEnvelope:
    """
    Tool Router 顶层入口。

    native tools    -> ctx.handlers[tool_name](tool_input, ctx)
    mcp__ tools     -> ctx.mcp_clients[server].call_tool(...)
    未知工具         -> ToolResultEnvelope.error(...)
    """
    if tool_name.startswith("mcp__"):
        return _run_mcp_tool(tool_name, tool_input, ctx)
    return _run_native_tool(tool_name, tool_input, ctx)


def _run_mcp_tool(tool_name: str, tool_input: dict, ctx: ToolUseContext) -> ToolResultEnvelope:
    parts = tool_name.split("__", 2)
    if len(parts) != 3:
        return ToolResultEnvelope.error(f"Invalid MCP tool name: {tool_name}")
    _, server_name, actual_tool = parts
    mcp_client = ctx.mcp_clients.get(server_name)
    if not mcp_client:
        return ToolResultEnvelope.error(f"MCP server not found: {server_name}")
    try:
        result = mcp_client.call_tool(actual_tool, tool_input)
        return ToolResultEnvelope.success(result)
    except Exception as e:
        return ToolResultEnvelope.error(f"MCP Error: {e}")


def _run_native_tool(tool_name: str, tool_input: dict, ctx: ToolUseContext) -> ToolResultEnvelope:
    handler = ctx.handlers.get(tool_name)
    if not handler:
        return ToolResultEnvelope.error(f"Unknown tool: {tool_name}")
    try:
        output = handler(tool_input, ctx)
        if isinstance(output, ToolResultEnvelope):
            return output
        return ToolResultEnvelope.success(str(output))
    except Exception as e:
        return ToolResultEnvelope.error(f"Error: {e}")


# ── 向后兼容：旧代码仍可调用 handle_tool_call ─────────────────────────────────

def handle_tool_call(tool_name: str, tool_input: dict, handlers: dict, mcp_router=None) -> str:
    """
    向后兼容包装。新代码请直接用 route_tool(tool_name, tool_input, ctx)。
    """
    if mcp_router is not None and mcp_router.is_mcp_tool(tool_name):
        return mcp_router.call(tool_name, tool_input)
    handler = handlers.get(tool_name)
    if handler:
        # 支持新式 (inp, ctx) 和旧式 (**kw) 两种签名
        try:
            import inspect
            sig = inspect.signature(handler)
            params = list(sig.parameters)
            if len(params) >= 2 and params[1] != "kwargs":
                dummy_ctx = ToolUseContext(handlers=handlers)
                return str(handler(tool_input, dummy_ctx))
            return str(handler(**tool_input))
        except Exception as e:
            return f"Error: {e}"
    return f"Unknown tool: {tool_name}"


def normalize_tool_result(tool_name: str, output: str, intent: dict | None = None) -> str:
    """把工具输出包装成发给模型的标准 JSON envelope。"""
    status = "error" if ("Error:" in output or "MCP Error:" in output) else "ok"
    payload = {
        "source":  (intent or {}).get("source", "native"),
        "server":  (intent or {}).get("server"),
        "tool":    (intent or {}).get("tool", tool_name),
        "risk":    (intent or {}).get("risk", "unknown"),
        "status":  status,
        "preview": output[:500],
        "content": output,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
