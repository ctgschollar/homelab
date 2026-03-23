"""Interactive action log browser. Launch with browse_log(path, entries)."""
from __future__ import annotations

import json
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Static
from textual.containers import ScrollableContainer


_EVENT_STYLES: dict[str, str] = {
    "action_taken":      "green",
    "plan_proposed":     "yellow",
    "plan_approved":     "bold green",
    "plan_cancelled":    "red",
    "tier_reasoning":    "cyan",
    "monitor_alert":     "bold red",
    "monitor_recovered": "bold green",
}


def _summary(entry: dict) -> str:
    event = entry.get("event", "")

    if event == "action_taken":
        tool = entry.get("tool", "?")
        outcome = entry.get("outcome", "")[:80]
        tier = entry.get("tier", "?")
        safe = " [safe mode]" if entry.get("safe_mode_active") else ""
        return f"{tool} tier={tier}{safe} — {outcome}"

    if event in ("plan_proposed", "plan_approved", "plan_cancelled"):
        plan_id = entry.get("plan_id", "?")
        tool = entry.get("tool", "?")
        reason = f" — {entry['reason']}" if "reason" in entry else ""
        return f"{plan_id} / {tool}{reason}"

    if event == "tier_reasoning":
        tool = entry.get("tool", "?")
        proposed = entry.get("agent_proposed_tier", "?")
        effective = entry.get("effective_tier", "?")
        reasoning = entry.get("reasoning", "")[:80]
        return f"{tool} proposed={proposed} effective={effective} — {reasoning}"

    if event == "monitor_alert":
        svc = entry.get("service", "?")
        r, d = entry.get("running", "?"), entry.get("desired", "?")
        err = entry.get("last_error", "")[:60]
        return f"{svc} {r}/{d} replicas — {err}"

    if event == "monitor_recovered":
        svc = entry.get("service", "?")
        dur = entry.get("down_duration_seconds", "?")
        return f"{svc} — down for {dur}s"

    skip = {"ts", "event"}
    return "  ".join(f"{k}={v}" for k, v in entry.items() if k not in skip)[:100]


class DetailScreen(ModalScreen):
    """Full-screen view of a single log entry."""

    BINDINGS = [Binding("escape,q", "dismiss", "Close", show=True)]

    def __init__(self, entry: dict) -> None:
        super().__init__()
        self._entry = entry

    def compose(self) -> ComposeResult:
        pretty = json.dumps(self._entry, indent=2, default=str)
        yield Header(show_clock=False)
        yield ScrollableContainer(Static(pretty, expand=True))
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"Entry detail — {self._entry.get('event', '?')}"


class LogBrowser(App):
    """Navigable log table. Enter to expand, q to quit."""

    TITLE = "Homelab Agent — Action Log"
    BINDINGS = [
        Binding("up,k", "scroll_up", "Up", show=True),
        Binding("down,j", "scroll_down", "Down", show=True),
        Binding("enter", "expand", "Expand", show=True),
        Binding("q,escape", "quit", "Quit", show=True),
    ]

    CSS = """
    DataTable { height: 1fr; }
    DetailScreen ScrollableContainer { padding: 1 2; }
    DetailScreen Static { color: $text; text-wrap: wrap; }
    """

    def __init__(self, entries: list[tuple[datetime | None, dict]]) -> None:
        super().__init__()
        self._entries = entries

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield DataTable()
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("#", "Time", "Event", "Detail")

        for i, (ts, entry) in enumerate(self._entries, start=1):
            ts_str = ts.strftime("%m-%d %H:%M:%S") if ts else "?"
            event = entry.get("event", "?")
            style = _EVENT_STYLES.get(event, "")
            styled_event = f"[{style}]{event}[/{style}]" if style else event
            table.add_row(str(i), ts_str, styled_event, _summary(entry), key=str(i))

        if self._entries:
            table.move_cursor(row=0)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._open_row(event.cursor_row)

    def action_expand(self) -> None:
        table = self.query_one(DataTable)
        self._open_row(table.cursor_row)

    def action_scroll_up(self) -> None:
        self.query_one(DataTable).action_scroll_up()

    def action_scroll_down(self) -> None:
        self.query_one(DataTable).action_scroll_down()

    def _open_row(self, row_index: int) -> None:
        if 0 <= row_index < len(self._entries):
            _, entry = self._entries[row_index]
            self.push_screen(DetailScreen(entry))

    def action_quit(self) -> None:
        self.exit()


async def browse_log(entries: list[tuple[datetime | None, dict]]) -> None:
    """Launch the interactive log browser. Blocks until the user quits."""
    await LogBrowser(entries).run_async()
