"""Turning live agents into a fire order.

Pane IDs are opaque; `wQ:p6M` tells the operator nothing. Terminal titles are
what they actually recognise, so the table is titles and the input is the row
number beside them.
"""

import re
from dataclasses import dataclass

from .duration import DurationError, parse_duration

SEPARATORS = re.compile(r"[,\s]+")


class PickerError(ValueError):
    """Raised when an answer cannot be turned into a plan."""


@dataclass
class Step:
    """One send: wait `after_seconds` past the previous step, then prompt."""

    pane_id: str
    title: str
    kind: str
    after_seconds: int
    prompt: str


def candidates(agents, workspace=None, self_pane=None):
    """Every promptable agent, ordered the way the workspace tree shows them."""
    rows = []
    for agent in agents:
        if workspace and agent.get("workspace_id") != workspace:
            continue
        pane_id = agent.get("pane_id")
        rows.append({
            "pane_id": pane_id,
            "kind": agent.get("agent") or "?",
            "status": agent.get("agent_status") or "unknown",
            "title": agent.get("terminal_title_stripped") or pane_id,
            "workspace_id": agent.get("workspace_id") or "",
            "tab_id": agent.get("tab_id") or "",
            "is_self": bool(self_pane) and pane_id == self_pane,
        })
    rows.sort(key=lambda r: (r["workspace_id"], r["tab_id"], r["pane_id"]))
    return rows


def _fields(text):
    return [part for part in SEPARATORS.split(str(text).strip()) if part]


def parse_selection(text, rows):
    """Row numbers in fire order, resolved to candidates."""
    fields = _fields(text)
    if not fields:
        raise PickerError("pick at least one pane")

    picked = []
    seen = set()
    for field in fields:
        if not field.isdigit():
            raise PickerError("not a row number: %s" % field)
        index = int(field)
        if index < 1 or index > len(rows):
            raise PickerError(
                "row %d is not in the table (1-%d)" % (index, len(rows))
            )
        row = rows[index - 1]
        if row["is_self"]:
            raise PickerError(
                "row %d is this pane; a stagger run cannot prompt itself" % index
            )
        if row["pane_id"] in seen:
            raise PickerError("pane %s picked twice" % row["pane_id"])
        seen.add(row["pane_id"])
        picked.append(row)
    return picked


def parse_gaps(text, count):
    """One value for every gap, or one value fanned out across all of them."""
    if count <= 0:
        return []

    fields = _fields(text)
    try:
        values = [parse_duration(field, allow_zero=True) for field in fields]
    except DurationError as exc:
        raise PickerError(str(exc))

    if len(values) == 1:
        return values * count
    if len(values) == count:
        return values
    expected = "1 gap" if count == 1 else "1 gap or %d gaps" % count
    raise PickerError("expected %s, got %d" % (expected, len(values)))


def build_plan(rows, start_seconds, gaps, prompt):
    """Steps carrying the delay that precedes each one."""
    if len(gaps) != max(len(rows) - 1, 0):
        raise PickerError(
            "expected %d gaps for %d panes, got %d"
            % (max(len(rows) - 1, 0), len(rows), len(gaps))
        )
    delays = [start_seconds] + list(gaps)
    return [
        Step(
            pane_id=row["pane_id"],
            title=row["title"],
            kind=row["kind"],
            after_seconds=delay,
            prompt=prompt,
        )
        for row, delay in zip(rows, delays)
    ]
