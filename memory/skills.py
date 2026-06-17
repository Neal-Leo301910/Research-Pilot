from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from config import SKILLS_DIR


@dataclass
class SkillManifest:
    name: str
    description: str
    path: Path


@dataclass
class SkillDocument:
    manifest: SkillManifest
    body: str


class SkillRegistry:
    """Cheap skill catalog with on-demand full body loading."""

    def __init__(self, skills_dir: Path = SKILLS_DIR):
        self.skills_dir = skills_dir
        self.documents: dict[str, SkillDocument] = {}
        self.reload()

    def reload(self) -> str:
        self.documents = {}
        if not self.skills_dir.exists():
            return "No skills directory found."
        for path in sorted(self.skills_dir.rglob("SKILL.md")):
            try:
                meta, body = self._parse_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            name = meta.get("name", path.parent.name)
            description = meta.get("description", "No description")
            manifest = SkillManifest(name=name, description=description, path=path)
            self.documents[name] = SkillDocument(manifest=manifest, body=body.strip())
        return f"Loaded {len(self.documents)} skills."

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        meta = {}
        for line in match.group(1).strip().splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
        return meta, match.group(2)

    def describe_available(self) -> str:
        if not self.documents:
            return "(no skills available)"
        lines = []
        for name in sorted(self.documents):
            manifest = self.documents[name].manifest
            lines.append(f"- {manifest.name}: {manifest.description}")
        return "\n".join(lines)

    def load_full_text(self, name: str) -> str:
        document = self.documents.get(name)
        if not document:
            known = ", ".join(sorted(self.documents)) or "(none)"
            return f"Error: Unknown skill '{name}'. Available skills: {known}"
        return f"<skill name=\"{document.manifest.name}\">\n{document.body}\n</skill>"
