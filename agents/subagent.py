from __future__ import annotations

from config import MODEL, client, WORKDIR
from core.messages import normalize_messages, extract_text
from core.compaction import persist_large_output
from tools.registry import BASE_FILE_TOOLS, SKILL_TOOL
from tools.bash import run_bash
from tools.files import run_read, run_write, run_edit

CHILD_TOOLS = [*BASE_FILE_TOOLS, SKILL_TOOL]


def child_tool_call(tool_name: str, tool_input: dict, skill_registry) -> str:
    if tool_name == "bash":
        return run_bash(tool_input["command"])
    if tool_name == "read_file":
        return run_read(tool_input["path"], tool_input.get("limit"))
    if tool_name == "write_file":
        return run_write(tool_input["path"], tool_input["content"])
    if tool_name == "edit_file":
        return run_edit(tool_input["path"], tool_input["old_text"], tool_input["new_text"])
    if tool_name == "load_skill":
        return skill_registry.load_full_text(tool_input["name"])
    return f"Unknown tool: {tool_name}"


def run_subagent(prompt: str, skill_registry, description: str = "subtask", max_turns: int = 30) -> str:
    sub_messages = [{"role": "user", "content": prompt}]
    sub_system = (
        f"You are a coding subagent at {WORKDIR}. Complete the delegated task, "
        "use tools as needed, then summarize findings and changes. Your context "
        "is isolated from the parent conversation."
    )
    response = None
    for _ in range(max_turns):
        response = client.messages.create(
            model=MODEL,
            system=sub_system,
            messages=normalize_messages(sub_messages),
            tools=CHILD_TOOLS,
            max_tokens=8000,
        )
        sub_messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                output = child_tool_call(block.name, block.input or {}, skill_registry)
            except Exception as e:
                output = f"Error: {e}"
            print(f"  [subagent:{description}] {block.name}: {str(output)[:120]}")
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": persist_large_output(block.id, str(output)),
                }
            )
        sub_messages.append({"role": "user", "content": results})
    if response is None:
        return "(subagent did not run)"
    return extract_text(response.content) or "(no summary)"
