from __future__ import annotations

import re
import subprocess
from pathlib import Path

from config import WORKDIR


class BashSecurityValidator:
    """Small bash validator for high-risk command patterns."""

    VALIDATORS = [
        ("shell_metachar", r"[;&|`$]"),
        ("sudo", r"\bsudo\b"),
        ("rm_rf", r"\brm\s+(-[a-zA-Z]*)?r"),
        ("cmd_substitution", r"\$\("),
        ("ifs_injection", r"\bIFS\s*="),
    ]

    def validate(self, command: str) -> list[tuple[str, str]]:
        failures = []
        for name, pattern in self.VALIDATORS:
            if re.search(pattern, command):
                failures.append((name, pattern))
        return failures

    def describe_failures(self, command: str) -> str:
        failures = self.validate(command)
        if not failures:
            return "No issues detected"
        parts = [f"{name} (pattern: {pattern})" for name, pattern in failures]
        return "Security flags: " + ", ".join(parts)


bash_validator = BashSecurityValidator()

_DANGEROUS_TOKENS = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]


def run_bash(command: str, cwd: Path = WORKDIR, timeout: int = 120) -> str:
    if any(token in command for token in _DANGEROUS_TOKENS):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (result.stdout + result.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: Timeout ({timeout}s)"
