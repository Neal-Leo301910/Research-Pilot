from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import (
    MODEL,
    client,
    TRANSCRIPT_DIR,
    TOOL_RESULTS_DIR,
    KEEP_RECENT_TOOL_RESULTS,
    PERSIST_THRESHOLD,
    PREVIEW_CHARS,
)
from core.messages import normalize_messages, collect_tool_result_blocks


@dataclass
class CompactState:
    has_compacted: bool = False
    last_summary: str = ""
    recent_files: list[str] = field(default_factory=list)


def collect_tool_result_blocks(messages: list) -> list[tuple[int, int, dict]]:
    from core.messages import content_block_to_dict
    blocks = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            block_dict = block if isinstance(block, dict) else content_block_to_dict(block)
            if isinstance(block_dict, dict) and block_dict.get("type") == "tool_result":
                blocks.append((message_index, block_index, block_dict))
    return blocks


def micro_compact(messages: list) -> list:
    tool_results = collect_tool_result_blocks(messages)
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages
    for message_index, block_index, block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        content = block.get("content", "")
        if not isinstance(content, str) or len(content) <= 120:
            continue
        block["content"] = "[Earlier tool result compacted. Re-run the tool if you need full detail.]"
        messages[message_index]["content"][block_index] = block
    return messages


def write_transcript(messages: list) -> Path:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for message in normalize_messages(messages):
            handle.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
    return path


def summarize_history(messages: list) -> str:
    conversation = json.dumps(normalize_messages(messages), ensure_ascii=False, default=str)[:80000]
    prompt = (
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve the current goal, important findings, files read or changed, "
        "remaining work, and user constraints.\n\n"
        f"{conversation}"
    )
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )
    return response.content[0].text.strip()


def compact_history(messages: list, state: CompactState, focus: str | None = None) -> list:
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    if focus:
        summary += f"\n\nFocus to preserve next: {focus}"
    if state.recent_files:
        recent_lines = "\n".join(f"- {path}" for path in state.recent_files)
        summary += f"\n\nRecent files to reopen if needed:\n{recent_lines}"
    state.has_compacted = True
    state.last_summary = summary
    return [
        {
            "role": "user",
            "content": "This conversation was compacted so the agent can continue working.\n\n" + summary,
        }
    ]


def auto_compact(messages: list, state: CompactState) -> list:
    try:
        return compact_history(messages, state, focus="Automatic recovery compaction.")
    except Exception as e:
        return [{"role": "user", "content": f"This session was compacted after an error, but compaction failed: {e}"}]


def track_recent_file(state: CompactState, path: str) -> None:
    if path in state.recent_files:
        state.recent_files.remove(path)
    state.recent_files.append(path)
    if len(state.recent_files) > 5:
        state.recent_files[:] = state.recent_files[-5:]


def persist_large_output(tool_use_id: str, output: str) -> str:
    if len(output) <= PERSIST_THRESHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stored_path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not stored_path.exists():
        stored_path.write_text(output, encoding="utf-8")
    from tools.files import display_path
    preview = output[:PREVIEW_CHARS]
    rel_path = display_path(stored_path)
    return (
        "<persisted-output>\n"
        f"Full output saved to: {rel_path}\n"
        "Preview:\n"
        f"{preview}\n"
        "</persisted-output>"
    )
