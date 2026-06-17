from __future__ import annotations

import json
from pathlib import Path

from config import WORKDIR, REPO_ROOT


class PluginLoader:
    def __init__(self, search_dirs: list[Path] | None = None):
        self.search_dirs = search_dirs or [WORKDIR, REPO_ROOT]
        self.plugins: dict[str, dict] = {}

    def scan(self) -> list[str]:
        found = []
        for search_dir in self.search_dirs:
            for dirname in (".claude-plugin", ".codex-plugin"):
                manifest_path = Path(search_dir) / dirname / "plugin.json"
                if not manifest_path.exists():
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    name = manifest.get("name", manifest_path.parent.parent.name)
                    self.plugins[name] = manifest
                    found.append(name)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"[Plugin] Failed to load {manifest_path}: {e}")
        return found

    def get_mcp_servers(self) -> dict:
        servers = {}
        for plugin_name, manifest in self.plugins.items():
            for server_name, config in manifest.get("mcpServers", {}).items():
                servers[f"{plugin_name}__{server_name}"] = config
        return servers
