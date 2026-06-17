"""
main.py  —  entry point

agent_loop() 实现 5-step 主循环，完整对应下面的状态转移图：

  用户请求
    |
    v
  QueryState 初始化
    |
    v
  ┌──────────────────────────────────────────────────────────────┐
  │  STEP 1  组装 API payload（三面并列）                         │
  │    system prompt  ← build_system_prompt()                    │
  │    messages       ← build_messages_pipeline()  (含 reminder) │
  │    tools          ← build_tool_pool()                        │
  └──────────────────────┬───────────────────────────────────────┘
                         |
                         v
  ┌──────────────────────────────────────────────────────────────┐
  │  STEP 2  调用模型  call_model_once()                          │
  │    ModelOverloadError   → compact_retry  + continue          │
  │    ModelTransportError  → transport_retry + sleep + continue  │
  └──────────────────────┬───────────────────────────────────────┘
                         |
              ┌──────────┴──────────┐
              v                     v
          max_tokens           tool_use
              |                     |
              v                     v
  ┌────────────────┐   ┌────────────────────────────────────────┐
  │  STEP 3        │   │  STEP 4  Tool Router                   │
  │  max_tokens    │   │    权限判断 → Hook 拦截/注入            │
  │  _recovery     │   │    partition_tool_calls() 分批         │
  │  + continue    │   │    safe batch   → 并发执行             │
  └────────────────┘   │    unsafe batch → 串行执行             │
                       │    按序落地 context modifier           │
                       │    tool_result 写回 messages           │
                       └──────────────────┬─────────────────────┘
                                          |
                                          v
                       ┌──────────────────────────────────────┐
                       │  STEP 5  QueryState 更新              │
                       │    stop_hook_continuation            │
                       │    compact_retry                     │
                       │    tool_result_continuation          │
                       └──────────────────────────────────────┘
"""
from __future__ import annotations

import json
import os
import time

try:
    import readline
    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass

# ── project imports ───────────────────────────────────────────────────────────
from config import (
    WORKDIR, REPO_ROOT, TEAM_DIR, INBOX_DIR, REQUESTS_DIR,
    TASKS_DIR, RUNTIME_DIR, SKILLS_DIR, MEMORY_DIR,
    PERMISSION_MODES, CONTINUATION_MESSAGE, TOKEN_THRESHOLD, CONTEXT_LIMIT,
)

from core.loop import QueryState
from core.messages import (
    extract_text, estimate_context_size, estimate_tokens,
    ReminderMessage, build_messages_pipeline,
)
from core.compaction import (
    CompactState, compact_history, micro_compact,
    auto_compact, track_recent_file, persist_large_output,
)
from core.model import (
    call_model_once, backoff_delay,
    ModelTransportError, ModelOverloadError,
)

from tools.registry import build_tool_pool
from tools.context import ToolUseContext
from tools.handlers import native_handlers, route_tool, normalize_tool_result
from tools.execution import partition_tool_calls, execute_tool_batches
from tools.files import display_path

from agents.todo import TodoManager
from agents.subagent import run_subagent
from agents.teammates import TeammateManager

from memory.manager import MemoryManager
from memory.dream import DreamConsolidator
from memory.skills import SkillRegistry

from tasks.manager import TaskManager
from tasks.claiming import claim_task
from tasks.background import BackgroundManager
from tasks.runtime import RuntimeTaskManager

from scheduling.cron import CronScheduler

from worktrees.manager import WorktreeManager
from worktrees.events import EventBus
from messaging.bus import MessageBus
from messaging.requests import (
    RequestStore, handle_shutdown_request,
    handle_plan_review, check_request_status,
)
from permissions.manager import PermissionManager
from hooks.manager import HookManager
from mcp.client import ScopedMcpServerConfig
from mcp.router import MCPToolRouter
from mcp.plugins import PluginLoader
from system.prompt import build_system_prompt, build_api_payload

# ── singletons ────────────────────────────────────────────────────────────────

COMPACT_STATE   = CompactState()
TODO            = TodoManager()
SKILL_REGISTRY  = SkillRegistry(SKILLS_DIR)
memory_mgr      = MemoryManager(MEMORY_DIR)
dream_consolidator = DreamConsolidator(MEMORY_DIR)
TASKS           = TaskManager(REPO_ROOT / ".tasks")
EVENTS          = EventBus(REPO_ROOT / ".worktrees" / "events.jsonl")
WORKTREES       = WorktreeManager(REPO_ROOT, TASKS, EVENTS)
BUS             = MessageBus(INBOX_DIR)
REQUEST_STORE   = RequestStore(REQUESTS_DIR)
RUNTIME_TASK_MGR = RuntimeTaskManager(RUNTIME_DIR)
BG              = BackgroundManager(RUNTIME_DIR, runtime_task_mgr=RUNTIME_TASK_MGR)
scheduler       = CronScheduler()
hook_manager    = HookManager()
permission_gate = PermissionManager(mode=os.getenv("PERMISSION_MODE", "default"))
mcp_router      = MCPToolRouter()
plugin_loader   = PluginLoader()
TEAM            = TeammateManager(TEAM_DIR, BUS, REQUEST_STORE, TASKS)


# ── ToolUseContext 工厂 ───────────────────────────────────────────────────────

def _make_tool_ctx(messages: list) -> ToolUseContext:
    """
    构造当前轮的 ToolUseContext。

    handlers、mcp_clients、permission_ctx、messages 都在这里注入，
    工具执行时通过 ctx 访问，而不是靠参数透传。
    """
    handlers = native_handlers(
        todo_mgr=TODO,
        skill_registry=SKILL_REGISTRY,
        memory_mgr=memory_mgr,
        dream_consolidator=dream_consolidator,
        tasks=TASKS,
        team=TEAM,
        bus=BUS,
        request_store=REQUEST_STORE,
        bg=BG,
        scheduler=scheduler,
        worktrees=WORKTREES,
        events=EVENTS,
        permission_gate=permission_gate,
        claim_task_fn=claim_task,
        handle_shutdown_request_fn=lambda tm: handle_shutdown_request(tm, BUS, REQUEST_STORE),
        handle_plan_review_fn=lambda rid, approve, fb="": handle_plan_review(rid, approve, fb, BUS, REQUEST_STORE),
        check_request_status_fn=lambda rid: check_request_status(rid, REQUEST_STORE),
        run_subagent_fn=lambda prompt, desc="subtask": run_subagent(prompt, SKILL_REGISTRY, desc),
    )
    return ToolUseContext(
        handlers=handlers,
        mcp_clients=mcp_router.clients,
        permission_ctx={
            "mode":  permission_gate.mode,
            "rules": permission_gate.rules,
        },
        messages=messages,
        cwd=WORKDIR,
    )


# ── STEP 1 helpers ────────────────────────────────────────────────────────────

def _collect_reminders() -> list[ReminderMessage]:
    """收集当前轮需要注入消息流的临时提醒（不进 system prompt）。"""
    reminders: list[ReminderMessage] = []
    reminder_text = TODO.reminder()
    if reminder_text:
        reminders.append(ReminderMessage(content=reminder_text, source="todo"))
    return reminders


def _inject_notifications(messages: list) -> None:
    """
    把 inbox / runtime task 完成通知 / cron 通知追加到 messages 里。

    通知来源：
      inbox            teammate 发来的消息
      RUNTIME_TASK_MGR 所有类型的运行时任务完成通知（local_bash / agent / teammate 等）
      BG               向后兼容：没有 RuntimeTaskManager 时的本地通知
      scheduler        cron 触发的 prompt 注入
      elicitations     MCP server 发起的用户输入请求
    """
    inbox = BUS.read_inbox("lead")
    if inbox:
        messages.append({"role": "user",      "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>"})
        messages.append({"role": "assistant", "content": "Noted inbox messages."})

    # 运行时任务完成通知（统一从 RuntimeTaskManager 消费）
    rt_notifications = RUNTIME_TASK_MGR.drain_notifications()
    if rt_notifications:
        lines = []
        for n in rt_notifications:
            wg = f" wg={n['work_graph_task_id']}" if n.get("work_graph_task_id") is not None else ""
            lines.append(
                f"[rt:{n['task_id']}][{n['type']}] {n['status']}: "
                f"{n['description'][:60]}{wg} -> {n['result_preview'][:100]}"
                + (f" (output={n['output_file']})" if n.get("output_file") else "")
            )
        messages.append({"role": "user", "content": f"<runtime-results>\n" + "\n".join(lines) + "\n</runtime-results>"})

    # 向后兼容：BG 自身的本地通知（当没有 RuntimeTaskManager 时）
    bg_notifications = BG.drain_notifications()
    if bg_notifications:
        text = "\n".join(
            f"[bg:{n['task_id']}] {n['status']}: {n.get('preview', '')} (output_file={n.get('output_file', '')})"
            for n in bg_notifications
        )
        messages.append({"role": "user", "content": f"<background-results>\n{text}\n</background-results>"})

    stalled = RUNTIME_TASK_MGR.detect_stalled()
    if stalled:
        messages.append({"role": "user", "content": f"<runtime-stalled>{json.dumps(stalled)}</runtime-stalled>"})

    for note in scheduler.drain_notifications():
        print(f"[Cron] {note[:100]}")
        messages.append({"role": "user", "content": note})

    # MCP elicitation 请求（服务器主动向用户请求输入）
    elicitations = mcp_router.drain_elicitations()
    for req in elicitations:
        print(f"[MCP Elicitation] {req.server_name}: {req.message[:120]}")
        messages.append({
            "role": "user",
            "content": f"<mcp-elicitation server=\"{req.server_name}\" id=\"{req.request_id}\">"
                       f"{req.message}</mcp-elicitation>",
        })


# ── STEP 4: Tool Router ───────────────────────────────────────────────────────

def _run_tool_router(
    response,
    messages: list,
    ctx: ToolUseContext,
) -> tuple[list, bool, bool, str | None]:
    """
    STEP 4 完整实现：

      1. 权限判断 + Hook 拦截/注入
      2. partition_tool_calls()  按并发安全性分批
      3. 安全批  → 并发执行
         不安全批 → 串行执行
      4. 按原始顺序落地 context modifier
      5. tool_result 写回 messages

    返回：
      results           list[dict]   tool_result 块列表
      used_todo         bool
      stop_hook_blocked bool
      compact_focus     str | None
    """
    tool_use_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]

    results:           list[dict] = []
    used_todo:         bool       = False
    stop_hook_blocked: bool       = False
    compact_focus:     str | None = None

    # ── 权限判断 + PreToolUse Hook（在分批执行前逐一检查）────────────────────
    permitted_blocks = []
    for block in tool_use_blocks:
        tool_input = dict(getattr(block, "input", None) or {})

        # PreToolUse hook
        hook_ctx = {"tool_name": block.name, "tool_input": tool_input}
        pre = hook_manager.run_hooks("PreToolUse", hook_ctx)
        for msg in pre.get("messages", []):
            results.append({"type": "text", "text": f"[Hook]: {msg}"})

        if pre.get("blocked"):
            output = f"Tool blocked by PreToolUse hook: {pre.get('block_reason', 'blocked')}"
            print(f"  [HOOK BLOCKED] {block.name}: {output[:160]}")
            stop_hook_blocked = True
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
            continue

        tool_input = hook_ctx.get("tool_input", tool_input)

        # 权限判断
        decision = permission_gate.check(block.name, tool_input)
        if pre.get("permission_override") in {"allow", "deny", "ask"}:
            decision["behavior"] = pre["permission_override"]

        if decision["behavior"] == "deny":
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"Permission denied: {decision['reason']}",
            })
            continue
        if decision["behavior"] == "ask" and not permission_gate.ask_user(decision["intent"], tool_input):
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"Permission denied by user: {decision['reason']}",
            })
            continue

        # 把通过权限检查的 block 更新 input（hook 可能已修改）
        block.input = tool_input
        permitted_blocks.append((block, decision))

    # ── 分批执行（并发安全 / 不安全）────────────────────────────────────────
    if permitted_blocks:
        plain_blocks = [b for b, _ in permitted_blocks]
        tool_results, all_tracked = execute_tool_batches(plain_blocks, ctx)

        # PostToolUse hook + 侧效标记
        tracked_map = {t.id: t for t in all_tracked}
        for block, decision in permitted_blocks:
            tracked = tracked_map.get(block.id)
            if not tracked or not tracked.result:
                continue
            output_str = tracked.result.content

            # PostToolUse hook
            post_ctx = {"tool_name": block.name, "tool_input": block.input, "tool_output": output_str}
            post = hook_manager.run_hooks("PostToolUse", post_ctx)
            for msg in post.get("messages", []):
                output_str += f"\n[Hook note]: {msg}"

            # 侧效追踪
            if block.name in {"read_file", "write_file", "edit_file"} and block.input.get("path"):
                track_recent_file(COMPACT_STATE, block.input["path"])
            if block.name == "todo":
                used_todo = True
            if block.name == "compact":
                compact_focus = block.input.get("focus")

            print(f"> {block.name}: {output_str[:200]}")

        results.extend(tool_results)

    return results, used_todo, stop_hook_blocked, compact_focus


# ── agent_loop ────────────────────────────────────────────────────────────────

def agent_loop(messages: list) -> None:
    state = QueryState(messages=messages)

    while True:

        # ════════════════════════════════════════════════════════════════════
        # STEP 1: 组装 API payload（三面并列：system | messages | tools）
        # ════════════════════════════════════════════════════════════════════
        messages[:] = micro_compact(messages)
        if estimate_context_size(messages) > CONTEXT_LIMIT:
            print("[pre-step] context limit → inline compact")
            messages[:] = compact_history(messages, COMPACT_STATE)

        _inject_notifications(messages)

        reminders = _collect_reminders()

        system   = build_system_prompt(WORKDIR, build_tool_pool(mcp_router), SKILL_REGISTRY, memory_mgr, permission_gate)
        msg_list = build_messages_pipeline(messages, reminders=reminders)
        tools    = build_tool_pool(mcp_router)

        payload = build_api_payload(system=system, messages=msg_list, tools=tools)

        # ════════════════════════════════════════════════════════════════════
        # STEP 2: 调用模型
        # ════════════════════════════════════════════════════════════════════
        try:
            response = call_model_once(
                messages=payload["messages"],
                system=payload["system"],
                tools=payload["tools"],
            )

        # ── compact_retry：prompt 过长 ─────────────────────────────────────
        except ModelOverloadError as e:
            if not state.budget_ok("compact_retry"):
                print(f"[agent_loop] compact_retry budget exhausted. {state.summary()}")
                return
            state.record_transition("compact_retry")
            print(f"[compact_retry #{state.compact_retry_count}] {e}")
            messages[:] = auto_compact(messages, COMPACT_STATE)
            continue

        # ── transport_retry：网络 / 基础设施抖动 ──────────────────────────
        except ModelTransportError as e:
            if not state.budget_ok("transport_retry"):
                print(f"[agent_loop] transport_retry budget exhausted. {state.summary()}")
                return
            state.record_transition("transport_retry")
            delay = backoff_delay(state.transport_retry_count)
            print(f"[transport_retry #{state.transport_retry_count}] sleeping {delay:.1f}s — {e}")
            time.sleep(delay)
            continue

        # ════════════════════════════════════════════════════════════════════
        # STEP 3: 处理模型返回
        # ════════════════════════════════════════════════════════════════════
        messages.append({"role": "assistant", "content": response.content})

        # ── max_tokens_recovery ────────────────────────────────────────────
        if response.stop_reason == "max_tokens":
            if not state.budget_ok("max_tokens_recovery"):
                print(f"[agent_loop] max_tokens_recovery budget exhausted. {state.summary()}")
                return
            state.record_transition("max_tokens_recovery")
            print(f"[max_tokens_recovery #{state.max_tokens_recovery_count}]")
            messages.append({"role": "user", "content": CONTINUATION_MESSAGE})
            continue

        # ── 普通回答，结束本次请求 ─────────────────────────────────────────
        if response.stop_reason != "tool_use":
            state.transition = None
            return

        # ════════════════════════════════════════════════════════════════════
        # STEP 4: Tool Router（权限 + Hook + 分批执行）
        # ════════════════════════════════════════════════════════════════════
        ctx = _make_tool_ctx(messages)
        results, used_todo, stop_hook_blocked, compact_focus = _run_tool_router(response, messages, ctx)

        # todo 计数更新
        if used_todo:
            TODO.state.rounds_since_update = 0
        else:
            TODO.note_round_without_update()

        # tool_result 写回 messages
        messages.append({"role": "user", "content": results})

        # ════════════════════════════════════════════════════════════════════
        # STEP 5: QueryState 更新 + 选择转移原因
        # ════════════════════════════════════════════════════════════════════

        # stop_hook_continuation：hook 拦截了工具
        if stop_hook_blocked:
            state.record_transition("stop_hook_continuation")
            print(f"[stop_hook_continuation] turn={state.turn_count}")
            continue

        # compact_retry：compact 工具被调用
        if compact_focus is not None:
            if not state.budget_ok("compact_retry"):
                print(f"[agent_loop] compact_retry budget exhausted. {state.summary()}")
                return
            state.record_transition("compact_retry")
            print(f"[compact_retry #{state.compact_retry_count}] manual compact, focus={compact_focus!r}")
            messages[:] = compact_history(messages, COMPACT_STATE, focus=compact_focus)
            continue

        # compact_retry：reactive token 阈值触发
        if estimate_tokens(messages) > TOKEN_THRESHOLD and not state.has_attempted_reactive_compact:
            if state.budget_ok("compact_retry"):
                state.record_transition("compact_retry")
                print(f"[compact_retry #{state.compact_retry_count}] reactive (token threshold)")
                messages[:] = auto_compact(messages, COMPACT_STATE)
                continue

        # tool_result_continuation：正常主线
        state.record_transition("tool_result_continuation")
        print(f"[agent_loop] {state.summary()}")


# ── REPL ──────────────────────────────────────────────────────────────────────

def print_response_text(history: list) -> None:
    final_text = extract_text(history[-1]["content"])
    if final_text:
        print(final_text)


def initialize_plugins() -> None:
    found = plugin_loader.scan()
    if found:
        print(f"[Plugins loaded: {', '.join(found)}]")
        for server_name, raw_config in plugin_loader.get_mcp_servers().items():
            cfg = ScopedMcpServerConfig.from_dict(server_name, raw_config, scope="project")
            from mcp.client import MCPClient
            mc = MCPClient(config=cfg)
            if mc.connect():
                mc.list_tools()
                mcp_router.register_client(mc)
                print(f"[MCP] Connected to {server_name} "
                      f"(tools={len(mc.capabilities.tools)}, "
                      f"resources={len(mc.capabilities.resources)}, "
                      f"elicitation={mc.capabilities.supports_elicitation})")
            else:
                print(f"[MCP] Failed to connect {server_name}: "
                      f"{mc.connection.status} — {mc.connection.error_message}")


def main() -> None:
    memory_mgr.load_all()
    initialize_plugins()
    scheduler.start()

    session_start = hook_manager.run_hooks("SessionStart", {"tool_name": "", "tool_input": {}})
    for msg in session_start.get("messages", []):
        print(f"[SessionStart hook] {msg[:200]}")

    print(f"Repo root: {REPO_ROOT}")
    print(f"Memory: {len(memory_mgr.memories)} memories loaded")
    if not WORKTREES.git_available:
        print("Note: not in a git repo — worktree_* tools will error.")
    print(f"Tools: {len(build_tool_pool(mcp_router))} ({len(mcp_router.get_all_tools())} from MCP)")
    print("Commands: /team /todo /skills /memories /tasks /runtime /background /cron /worktrees /mcp /compact /perm default|plan|auto")

    history: list = []
    while True:
        try:
            query = input("\033[36mintegrated >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        stripped = query.strip()
        if stripped.lower() in ("q", "quit", "exit", ""):
            break

        _DISPATCH = {
            "/team":       lambda: print(TEAM.list_all()),
            "/todo":       lambda: print(TODO.render()),
            "/skills":     lambda: print(SKILL_REGISTRY.describe_available()),
            "/memories":   lambda: print(memory_mgr.list_memories()),
            "/inbox":      lambda: print(json.dumps(BUS.read_inbox("lead"), indent=2)),
            "/tasks":      lambda: print(TASKS.list_all()),
            "/background": lambda: print(BG.check()),
            "/cron":       lambda: print(scheduler.list_tasks()),
            "/test-cron":  lambda: print(scheduler.enqueue_test()),
            "/worktrees":  lambda: print(WORKTREES.list_all()),
            "/events":     lambda: print(EVENTS.list_recent()),
            "/rules":      lambda: [print(f"  {i}: {r}") for i, r in enumerate(permission_gate.rules)],
            "/hooks":      lambda: print(json.dumps(hook_manager.hooks, indent=2)),
            "/runtime":    lambda: print(RUNTIME_TASK_MGR.list_all()),
            "/mcp":        lambda: print(json.dumps(mcp_router.get_connection_states(), indent=2)),
            "/compact":    lambda: (
                history.__setitem__(slice(None), compact_history(history, COMPACT_STATE, focus="Manual CLI compact.")),
                print("Conversation compacted.")
            ),
        }
        if stripped in _DISPATCH:
            _DISPATCH[stripped]()
            continue
        if stripped.startswith(("/perm ", "/mode ")):
            mode = stripped.split(maxsplit=1)[1]
            permission_gate.mode = mode if mode in PERMISSION_MODES else permission_gate.mode
            dream_consolidator.mode = permission_gate.mode
            print(f"Permission mode: {permission_gate.mode}")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history)
        print_response_text(history)
        print()

    for mc in mcp_router.clients.values():
        mc.disconnect()
    scheduler.stop()


if __name__ == "__main__":
    main()
