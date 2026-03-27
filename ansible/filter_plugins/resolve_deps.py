# ansible/filter_plugins/resolve_deps.py
from __future__ import annotations

from ansible.errors import AnsibleFilterError


def resolve_tool_deps(required_tools: list, tool_definitions: dict) -> list:
    """
    Depth-first topological sort of required_tools using tool_definitions deps.
    Returns a deduplicated ordered list where every dependency precedes its dependent.
    First-visit wins for deduplication.
    """
    visited: set = set()
    result: list = []

    def visit(name: str, stack: list) -> None:
        if name in stack:
            raise AnsibleFilterError(
                f"Dependency cycle detected: {' -> '.join(stack + [name])}"
            )
        if name in visited:
            return
        if name not in tool_definitions:
            raise AnsibleFilterError(
                f"Unknown tool: '{name}'. Available: {sorted(tool_definitions.keys())}"
            )
        for dep in tool_definitions[name].get("deps", []):
            visit(dep, stack + [name])
        visited.add(name)
        result.append(name)

    for tool in required_tools:
        visit(tool, [])

    return result


def tools_with_key(resolved_tools: list, tool_definitions: dict, key: str) -> list:
    """
    Return the subset of resolved_tools whose tool_definitions entry contains key.
    Preserves order of resolved_tools.
    """
    return [t for t in resolved_tools if key in tool_definitions.get(t, {})]


class FilterModule:
    def filters(self) -> dict:
        return {
            "resolve_tool_deps": resolve_tool_deps,
            "tools_with_key": tools_with_key,
        }
