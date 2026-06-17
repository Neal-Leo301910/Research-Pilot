from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from config import HOOK_CONFIG_FILE, HOOK_EVENTS, HOOK_TIMEOUT, WORKDIR
from permissions.manager import is_workspace_trusted


class HookManager:
    """Load and execute hooks from .hooks.json for SessionStart/PreToolUse/PostToolUse."""

    def __init__(self, config_path: Path | None = None, sdk_mode: bool = False):
        self.hooks: dict[str, list] = {e: [] for e in HOOK_EVENTS}
        self._sdk_mode = sdk_mode
        self.config_path = config_path or HOOK_CONFIG_FILE
        self.reload()

    def reload(self) -> str:
        self.hooks = {e: [] for e in HOOK_EVENTS}
        if not self.config_path.exists():
            return "No hook config found."
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            for event in HOOK_EVENTS:
                self.hooks[event] = config.get("hooks", {}).get(event, [])
            return f"Hooks loaded from {self.config_path}"
        except Exception as e:
            return f"Hook config error: {e}"

    def _check_workspace_trust(self) -> bool:
        return self._sdk_mode or is_workspace_trusted()

    def run_hooks(self, event: str, context: dict | None = None) -> dict:
        result: dict = {"blocked": False, "messages": []}
        if not self._check_workspace_trust():
            return result

        for hook_def in self.hooks.get(event, []):
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name", "")
                if matcher != "*" and matcher != tool_name:
                    continue

            command = hook_def.get("command", "")
            if not command:
                continue

            env = dict(os.environ)
            if context:
                env["HOOK_EVENT"] = event
                env["HOOK_TOOL_NAME"] = context.get("tool_name", "")
                env["HOOK_TOOL_INPUT"] = json.dumps(
                    context.get("tool_input", {}), ensure_ascii=False
                )[:10000]
                if "tool_output" in context:
                    env["HOOK_TOOL_OUTPUT"] = str(context["tool_output"])[:10000]

            try:
                run = subprocess.run(
                    command,
                    shell=True,
                    cwd=WORKDIR,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=HOOK_TIMEOUT,
                )
                if run.returncode == 0:
                    if run.stdout.strip():
                        print(f"  [hook:{event}] {run.stdout.strip()[:100]}")
                    try:
                        hook_output = json.loads(run.stdout)
                        if "updatedInput" in hook_output and context:
                            context["tool_input"] = hook_output["updatedInput"]
                        if "additionalContext" in hook_output:
                            result["messages"].append(hook_output["additionalContext"])
                        if "permissionDecision" in hook_output:
                            result["permission_override"] = hook_output["permissionDecision"]
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif run.returncode == 1:
                    result["blocked"] = True
                    result["block_reason"] = run.stderr.strip() or "Blocked by hook"
                    print(f"  [hook:{event}] BLOCKED: {result['block_reason'][:200]}")
                elif run.returncode == 2:
                    message = run.stderr.strip()
                    if message:
                        result["messages"].append(message)
                        print(f"  [hook:{event}] INJECT: {message[:200]}")
            except subprocess.TimeoutExpired:
                print(f"  [hook:{event}] Timeout ({HOOK_TIMEOUT}s)")
            except Exception as e:
                print(f"  [hook:{event}] Error: {e}")

        return result
