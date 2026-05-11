"""Tests for HintEngine."""
import pytest
import yaml
from pathlib import Path
from agent.hints import HintEngine


def write_hint(hints_dir: Path, tool: str, name: str, pattern: str, hint: str) -> None:
    tool_dir = hints_dir / tool
    tool_dir.mkdir(parents=True, exist_ok=True)
    (tool_dir / f"{name}.yaml").write_text(yaml.dump({"pattern": pattern, "hint": hint}))


def test_no_hints_dir_is_noop(tmp_path: Path) -> None:
    engine = HintEngine(str(tmp_path / "nonexistent"))
    assert engine.enrich("run_shell", "some error output") == "some error output"


def test_no_match_returns_original(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "test", "SPECIFIC_ERROR", "hint text")
    engine = HintEngine(str(tmp_path))
    assert engine.enrich("run_shell", "completely different output") == "completely different output"


def test_matching_hint_appended(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "test", "SPECIFIC_ERROR", "hint text")
    engine = HintEngine(str(tmp_path))
    result = engine.enrich("run_shell", "output with SPECIFIC_ERROR inside")
    assert "[HINT: test]" in result
    assert "hint text" in result


def test_original_text_preserved_before_hint(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "test", "ERR", "fix it")
    engine = HintEngine(str(tmp_path))
    result = engine.enrich("run_shell", "got ERR")
    assert result.startswith("got ERR")


def test_no_hints_for_other_tool(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "test", "ERR", "fix it")
    engine = HintEngine(str(tmp_path))
    assert engine.enrich("docker_service_list", "some output with ERR") == "some output with ERR"


def test_multiple_matching_hints_all_appended(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "hint_a", "ERROR_A", "fix A")
    write_hint(tmp_path, "run_shell", "hint_b", "ERROR_B", "fix B")
    engine = HintEngine(str(tmp_path))
    result = engine.enrich("run_shell", "output with ERROR_A and ERROR_B")
    assert "[HINT: hint_a]" in result
    assert "[HINT: hint_b]" in result


def test_hints_appended_in_filename_order(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "aaa", "MATCH", "first")
    write_hint(tmp_path, "run_shell", "zzz", "MATCH", "last")
    engine = HintEngine(str(tmp_path))
    result = engine.enrich("run_shell", "MATCH")
    assert result.index("[HINT: aaa]") < result.index("[HINT: zzz]")


def test_non_matching_hint_not_appended(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "aaa", "MATCH", "hint")
    write_hint(tmp_path, "run_shell", "bbb", "NO_MATCH", "other hint")
    engine = HintEngine(str(tmp_path))
    result = engine.enrich("run_shell", "only MATCH here")
    assert "[HINT: aaa]" in result
    assert "[HINT: bbb]" not in result


def test_plain_string_works_as_literal_match(tmp_path: Path) -> None:
    write_hint(tmp_path, "run_shell", "linstor", "VolumeDriver.Mount: PathIsDevice failed", "fix it")
    engine = HintEngine(str(tmp_path))
    result = engine.enrich("run_shell", "error: VolumeDriver.Mount: PathIsDevice failed for path")
    assert "[HINT: linstor]" in result
