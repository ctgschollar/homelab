from pathlib import Path
import re


def remove_stanza(text: str, zone_name: str) -> tuple[str, bool]:
    pattern = re.compile(
        r"\n?" + re.escape(zone_name) + r":\d+\s*\{[^}]*\}\n?",
        re.DOTALL,
    )
    new_text, count = pattern.subn("", text)
    new_text = re.sub(r"\n{3,}", "\n\n", new_text)
    return new_text, count > 0


def update_corefile(path: Path, zone_name: str) -> bool:
    text = path.read_text()
    new_text, found = remove_stanza(text, zone_name)
    if found:
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(new_text)
        tmp.replace(path)
    return found
