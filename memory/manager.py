from __future__ import annotations

import re
from pathlib import Path

from config import MEMORY_DIR, MEMORY_INDEX, MEMORY_TYPES, MAX_INDEX_LINES


class MemoryManager:
    """Persistent cross-session memories as markdown files plus MEMORY.md index."""

    def __init__(self, memory_dir: Path = MEMORY_DIR):
        self.memory_dir = memory_dir
        self.memories: dict[str, dict] = {}

    def load_all(self):
        self.memories = {}
        if not self.memory_dir.exists():
            return
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            parsed = self._parse_frontmatter(md_file.read_text(encoding="utf-8"))
            if parsed:
                name = parsed.get("name", md_file.stem)
                self.memories[name] = {
                    "description": parsed.get("description", ""),
                    "type": parsed.get("type", "project"),
                    "content": parsed.get("content", ""),
                    "file": md_file.name,
                }

    def load_memory_prompt(self) -> str:
        if not self.memories:
            return ""
        sections = ["# Memories (persistent across sessions)", ""]
        for mem_type in MEMORY_TYPES:
            typed = {k: v for k, v in self.memories.items() if v["type"] == mem_type}
            if not typed:
                continue
            sections.append(f"## [{mem_type}]")
            for name, memory in typed.items():
                sections.append(f"### {name}: {memory['description']}")
                if memory["content"].strip():
                    sections.append(memory["content"].strip())
                sections.append("")
        return "\n".join(sections)

    def save_memory(self, name: str, description: str, mem_type: str, content: str) -> str:
        if mem_type not in MEMORY_TYPES:
            return f"Error: type must be one of {MEMORY_TYPES}"
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
        if not safe_name:
            return "Error: invalid memory name"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"{safe_name}.md"
        file_path = self.memory_dir / file_name
        file_path.write_text(
            f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n{content}\n",
            encoding="utf-8",
        )
        self.memories[name] = {"description": description, "type": mem_type, "content": content, "file": file_name}
        self._rebuild_index()
        return f"Saved memory '{name}' [{mem_type}] to {file_path.relative_to(self.memory_dir.parent)}"

    def list_memories(self) -> str:
        if not self.memories:
            return "(no memories)"
        return "\n".join(
            f"  [{memory['type']}] {name}: {memory['description']}"
            for name, memory in self.memories.items()
        )

    def _rebuild_index(self):
        lines = ["# Memory Index", ""]
        for name, memory in self.memories.items():
            lines.append(f"- {name}: {memory['description']} [{memory['type']}]")
            if len(lines) >= MAX_INDEX_LINES:
                lines.append(f"... (truncated at {MAX_INDEX_LINES} lines)")
                break
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        MEMORY_INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _parse_frontmatter(self, text: str) -> dict | None:
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            return None
        header, body = match.group(1), match.group(2)
        result = {"content": body.strip()}
        for line in header.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
        return result
