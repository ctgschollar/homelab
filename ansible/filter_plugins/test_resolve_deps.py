# ansible/filter_plugins/test_resolve_deps.py
import pytest
from ansible.errors import AnsibleFilterError


TOOL_DEFS = {
    "gnupg": {"apt": ["gnupg"], "deps": []},
    "python3-pip": {"apt": ["python3-pip"], "deps": []},
    "pipx": {"apt": ["pipx"], "deps": ["python3-pip"]},
    "hatch": {"pipx": "hatch", "deps": ["pipx"]},
    "gh": {"deps": ["gnupg"], "tasks": "install-gh.yml"},
}


def resolve(tools):
    from resolve_deps import resolve_tool_deps
    return resolve_tool_deps(tools, TOOL_DEFS)


def with_key(tools, key):
    from resolve_deps import tools_with_key
    return tools_with_key(tools, TOOL_DEFS, key)


class TestResolveToolDeps:
    def test_single_tool_no_deps(self):
        assert resolve(["gnupg"]) == ["gnupg"]

    def test_single_tool_with_dep(self):
        assert resolve(["gh"]) == ["gnupg", "gh"]

    def test_chain(self):
        assert resolve(["hatch"]) == ["python3-pip", "pipx", "hatch"]

    def test_multiple_tools_shared_dep_deduped(self):
        result = resolve(["gnupg", "gh"])
        assert result.count("gnupg") == 1
        assert result.index("gnupg") < result.index("gh")

    def test_ordering_deps_before_dependents(self):
        result = resolve(["gh", "hatch"])
        assert result.index("gnupg") < result.index("gh")
        assert result.index("python3-pip") < result.index("pipx")
        assert result.index("pipx") < result.index("hatch")

    def test_full_example_from_spec(self):
        result = resolve(["gh", "hatch"])
        assert result == ["gnupg", "gh", "python3-pip", "pipx", "hatch"]

    def test_empty_input(self):
        assert resolve([]) == []

    def test_unknown_tool_raises(self):
        with pytest.raises(AnsibleFilterError, match="Unknown tool"):
            resolve(["nonexistent"])

    def test_cycle_raises(self):
        cyclic = {
            "a": {"deps": ["b"], "apt": ["a"]},
            "b": {"deps": ["a"], "apt": ["b"]},
        }
        from resolve_deps import resolve_tool_deps
        with pytest.raises(AnsibleFilterError, match="cycle"):
            resolve_tool_deps(["a"], cyclic)


class TestToolsWithKey:
    def test_apt_tools(self):
        resolved = ["gnupg", "gh", "python3-pip", "pipx", "hatch"]
        assert with_key(resolved, "apt") == ["gnupg", "python3-pip", "pipx"]

    def test_pipx_tools(self):
        resolved = ["gnupg", "gh", "python3-pip", "pipx", "hatch"]
        assert with_key(resolved, "pipx") == ["hatch"]

    def test_tasks_tools(self):
        resolved = ["gnupg", "gh", "python3-pip", "pipx", "hatch"]
        assert with_key(resolved, "tasks") == ["gh"]

    def test_empty_resolved(self):
        assert with_key([], "apt") == []

    def test_key_not_present_in_any(self):
        assert with_key(["gnupg"], "tasks") == []
