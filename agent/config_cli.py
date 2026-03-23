#!/usr/bin/env python3
"""
Config editor for the homelab agent. Preserves comments and env-var placeholders.

Usage:
  python config_cli.py show
  python config_cli.py get safety.global_safe_mode
  python config_cli.py set safety.global_safe_mode true
  python config_cli.py set safety.tool_tiers.run_shell 2
  python config_cli.py set safety.tool_tiers.docker_stack_deploy agent
  python config_cli.py safemode on|off
  python config_cli.py safe-resource add stack|service|node <value>
  python config_cli.py safe-resource remove stack|service|node <value>
  python config_cli.py safe-resource list
  python config_cli.py log-reasoning on|off
  python config_cli.py pricing <input_per_mtok> <output_per_mtok>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from ruamel.yaml import YAML

CONFIG_PATH = Path(__file__).parent / "config.yaml"

_VALID_TIERS = {1, 2, 3, "agent"}
_PLACEHOLDER_RE = re.compile(r"\$\{[^}]+\}")

# Pricing per million tokens (USD). Update when Anthropic changes prices.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001":  (1.0,  5.0),
    "claude-sonnet-4-20250514":   (3.0,  15.0),
    "claude-opus-4-20250514":     (15.0, 75.0),
}

yaml = YAML()
yaml.preserve_quotes = True


# ---------------------------------------------------------------------------
# Load / save helpers
# ---------------------------------------------------------------------------

def _load() -> tuple[object, Path]:
    path = CONFIG_PATH
    with open(path) as f:
        data = yaml.load(f)
    return data, path


def _save(data: object, path: Path) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f)


def _get_nested(data: dict, key_path: str) -> object:
    parts = key_path.split(".")
    node = data
    for part in parts:
        node = node[part]
    return node


def _set_nested(data: dict, key_path: str, value: object) -> None:
    parts = key_path.split(".")
    node = data
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def _coerce_value(raw: str) -> object:
    """Parse a string CLI value into bool / int / str without touching placeholders."""
    if _PLACEHOLDER_RE.fullmatch(raw):
        return raw  # preserve as-is
    low = raw.lower()
    if low in ("true", "on"):
        return True
    if low in ("false", "off"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    return raw  # string


def _validate_tier(value: object) -> None:
    if value not in _VALID_TIERS:
        print(f"ERROR: tier must be one of {_VALID_TIERS}, got {value!r}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_show(_args: list[str]) -> None:
    data, _ = _load()
    yaml.dump(data, sys.stdout)


def cmd_get(args: list[str]) -> None:
    if not args:
        print("Usage: config_cli.py get <key.path>")
        sys.exit(1)
    data, _ = _load()
    value = _get_nested(data, args[0])
    print(value)


def cmd_set(args: list[str]) -> None:
    if len(args) < 2:
        print("Usage: config_cli.py set <key.path> <value>")
        sys.exit(1)
    key_path, raw_value = args[0], args[1]
    value = _coerce_value(raw_value)

    # Validate if it looks like a tier key
    if "tool_tiers" in key_path:
        # value should be 1, 2, 3, or "agent"
        if isinstance(value, str) and value != "agent":
            print(f"ERROR: tier string must be 'agent', got {value!r}")
            sys.exit(1)
        if isinstance(value, int):
            _validate_tier(value)

    data, path = _load()
    old = _get_nested(data, key_path)
    _set_nested(data, key_path, value)

    if key_path == "anthropic.model" and isinstance(value, str):
        pricing = MODEL_PRICING.get(value)
        if pricing:
            data["anthropic"]["input_cost_per_mtok"] = pricing[0]
            data["anthropic"]["output_cost_per_mtok"] = pricing[1]
            print(f"  input_cost_per_mtok  → {pricing[0]}")
            print(f"  output_cost_per_mtok → {pricing[1]}")
        else:
            print(f"  WARNING: no pricing known for {value!r} — update MODEL_PRICING in config_cli.py")

    _save(data, path)
    print(f"  {key_path}: {old!r} → {value!r}")


def cmd_safemode(args: list[str]) -> None:
    if not args or args[0].lower() not in ("on", "off"):
        print("Usage: config_cli.py safemode on|off")
        sys.exit(1)
    enabled = args[0].lower() == "on"
    data, path = _load()
    old = data["safety"]["global_safe_mode"]
    data["safety"]["global_safe_mode"] = enabled
    _save(data, path)
    state = "ON" if enabled else "OFF"
    print(f"  global_safe_mode: {old!r} → {enabled!r}  ({state})")


def cmd_safe_resource(args: list[str]) -> None:
    if not args:
        print("Usage: config_cli.py safe-resource add|remove|list [stack|service|node] [value]")
        sys.exit(1)

    action = args[0].lower()
    data, path = _load()
    resources = data["safety"]["safe_mode_resources"]

    if action == "list":
        for key in ("stacks", "services", "nodes"):
            items = resources.get(key, [])
            print(f"  {key}: {list(items)}")
        return

    if len(args) < 3:
        print("Usage: config_cli.py safe-resource add|remove stack|service|node <value>")
        sys.exit(1)

    kind_map = {"stack": "stacks", "service": "services", "node": "nodes"}
    kind = kind_map.get(args[1].lower())
    if kind is None:
        print(f"ERROR: resource kind must be stack, service, or node — got {args[1]!r}")
        sys.exit(1)
    value = args[2]

    lst: list = list(resources.get(kind, []))
    if action == "add":
        if value not in lst:
            lst.append(value)
            resources[kind] = lst
            _save(data, path)
            print(f"  Added {value!r} to {kind}.")
        else:
            print(f"  {value!r} already in {kind}.")
    elif action == "remove":
        if value in lst:
            lst.remove(value)
            resources[kind] = lst
            _save(data, path)
            print(f"  Removed {value!r} from {kind}.")
        else:
            print(f"  {value!r} not found in {kind}.")
    else:
        print(f"ERROR: unknown action {action!r}")
        sys.exit(1)


def cmd_pricing(args: list[str]) -> None:
    if len(args) < 2:
        print("Usage: config_cli.py pricing <input_per_mtok> <output_per_mtok>")
        print("  e.g. config_cli.py pricing 3.0 15.0")
        sys.exit(1)
    try:
        input_cost = float(args[0])
        output_cost = float(args[1])
    except ValueError:
        print("ERROR: costs must be numbers (USD per million tokens)")
        sys.exit(1)
    data, path = _load()
    old_in = data["anthropic"].get("input_cost_per_mtok", "unset")
    old_out = data["anthropic"].get("output_cost_per_mtok", "unset")
    data["anthropic"]["input_cost_per_mtok"] = input_cost
    data["anthropic"]["output_cost_per_mtok"] = output_cost
    _save(data, path)
    print(f"  input_cost_per_mtok:  {old_in!r} → {input_cost}")
    print(f"  output_cost_per_mtok: {old_out!r} → {output_cost}")


def cmd_log_reasoning(args: list[str]) -> None:
    if not args or args[0].lower() not in ("on", "off"):
        print("Usage: config_cli.py log-reasoning on|off")
        sys.exit(1)
    enabled = args[0].lower() == "on"
    data, path = _load()
    old = data["safety"]["log_agent_tier_reasoning"]
    data["safety"]["log_agent_tier_reasoning"] = enabled
    _save(data, path)
    print(f"  log_agent_tier_reasoning: {old!r} → {enabled!r}")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

COMMANDS = {
    "show": cmd_show,
    "get": cmd_get,
    "set": cmd_set,
    "safemode": cmd_safemode,
    "safe-resource": cmd_safe_resource,
    "log-reasoning": cmd_log_reasoning,
    "pricing": cmd_pricing,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    command = sys.argv[1]
    rest = sys.argv[2:]

    handler = COMMANDS.get(command)
    if handler is None:
        print(f"ERROR: unknown command {command!r}")
        print(f"Available: {', '.join(COMMANDS)}")
        sys.exit(1)

    handler(rest)


if __name__ == "__main__":
    main()
