import logging
import re
from pathlib import Path

import yaml

logger = logging.getLogger("homelab.hints")


class HintEngine:
    def __init__(self, hints_dir: str) -> None:
        self._hints: dict[str, list[tuple[re.Pattern, str, str]]] = {}
        hints_path = Path(hints_dir)
        if not hints_path.exists():
            logger.debug("Hints directory %r not found — no hints loaded", hints_dir)
            return
        for tool_dir in sorted(hints_path.iterdir()):
            if not tool_dir.is_dir():
                continue
            entries: list[tuple[re.Pattern, str, str]] = []
            for hint_file in sorted(tool_dir.glob("*.yaml")):
                try:
                    data = yaml.safe_load(hint_file.read_text())
                    entries.append((re.compile(data["pattern"]), data["hint"], hint_file.stem))
                except Exception as exc:
                    logger.warning("Failed to load hint %s: %s", hint_file, exc)
            if entries:
                self._hints[tool_dir.name] = entries

    def enrich(self, tool_name: str, result: str) -> str:
        additions = [
            f"\n\n[HINT: {name}]\n{hint_text}"
            for pattern, hint_text, name in self._hints.get(tool_name, [])
            if pattern.search(result)
        ]
        return result + "".join(additions)
