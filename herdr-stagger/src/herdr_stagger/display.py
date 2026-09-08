"""Everything the operator reads: the tables, the live block, the summary.

The live block redraws in place on a terminal and degrades to one line per
event anywhere else. A tool that writes cursor motion into a log file is a
tool whose logs cannot be read.
"""

import time

from .duration import format_duration
from .runner import CANCELLED, FAILED, GAVE_UP, PENDING, SENT

TITLE_WIDTH = 42

MARKERS = {
    SENT: "✓",
    FAILED: "✗",
    GAVE_UP: "⊘",
    CANCELLED: "–",
    PENDING: " ",
}

TALLY_LABELS = [
    (SENT, "sent"),
    (FAILED, "failed"),
    (GAVE_UP, "gave up"),
    (CANCELLED, "cancelled"),
]


def _clock(unix):
    return time.strftime("%H:%M:%S", time.localtime(unix))


def _trim(text, width=TITLE_WIDTH):
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


def candidate_table(rows):
    """The numbered pane list the operator picks from."""
    width = max([len(r["pane_id"]) for r in rows] + [4])
    lines = ["  %2s  %-*s  %-6s %-8s %s" % ("#", width, "pane", "kind", "status", "title")]
    for number, row in enumerate(rows, 1):
        title = "(this pane)" if row["is_self"] else _trim(row["title"])
        lines.append(
            "  %2d  %-*s  %-6s %-8s %s"
            % (number, width, row["pane_id"], row["kind"], row["status"], title)
        )
    return "\n".join(lines)


def plan_table(steps):
    """Earliest fire times, cumulative from launch."""
    width = max([len(s.pane_id) for s in steps] + [4])
    lines = []
    elapsed = 0
    for step in steps:
        elapsed += step.after_seconds
        lines.append(
            "  t+%-9s %-*s  %s"
            % (format_duration(elapsed), width, step.pane_id, _trim(step.title))
        )
    lines.append("")
    lines.append("  Times are the earliest: a pane held at its turn shifts the rest.")
    return "\n".join(lines)


def status_text(step, result):
    """The right-hand column for a step that has resolved."""
    if result.outcome == SENT:
        held = " (held %s)" % format_duration(result.held_seconds) if result.held_seconds else ""
        return "SENT %s%s" % (_clock(result.at), held)
    if result.outcome == FAILED:
        return "FAILED %s" % (result.error or "")
    if result.outcome == GAVE_UP:
        return "GAVE UP %s" % (result.error or "")
    if result.outcome == CANCELLED:
        return "CANCELLED"
    return "+%s" % format_duration(step.after_seconds)


class Display:
    """Observer for the runner: redraws on a terminal, logs a line elsewhere."""

    def __init__(self, steps, out, tty=False):
        self.steps = steps
        self.out = out
        self.tty = tty
        self.width = max([len(s.pane_id) for s in steps] + [4])
        self.drawn = 0
        self.reported = set()

    def __call__(self, run):
        if self.tty:
            self._redraw(run)
        else:
            self._log_new(run)

    # --- plain stream -------------------------------------------------------

    def _log_new(self, run):
        for index, result in enumerate(run.results):
            if result.outcome == PENDING or index in self.reported:
                continue
            self.reported.add(index)
            step = self.steps[index]
            print(
                "[herdr-stagger] %s  %-*s  %s"
                % (_clock(result.at or time.time()), self.width, step.pane_id,
                   status_text(step, result)),
                file=self.out,
                flush=True,
            )

    # --- terminal -----------------------------------------------------------

    def _redraw(self, run):
        lines = self._lines(run)
        if self.drawn:
            self.out.write("\x1b[%dA" % self.drawn)
        for line in lines:
            self.out.write("\x1b[2K" + line + "\n")
        self.out.flush()
        self.drawn = len(lines)

    def _lines(self, run):
        header = "herdr-stagger — %d steps, started %s" % (
            len(self.steps), _clock(run.started_at)
        )
        lines = [header, ""]
        for index, step in enumerate(self.steps):
            result = run.results[index]
            current = index == run.index and result.outcome == PENDING
            marker = "▶" if current and run.phase != "done" else MARKERS[result.outcome]
            lines.append(
                "  %s %-*s  %-30s %s"
                % (marker, self.width, step.pane_id,
                   self._column(step, result, run, current), _trim(step.title, 34))
            )
        lines.append("")
        lines.append(
            "  Ctrl-C cancels the remaining steps."
            if run.phase != "done" else "  Run finished."
        )
        return lines

    def _column(self, step, result, run, current):
        if result.outcome != PENDING:
            return status_text(step, result)
        if not current or run.phase == "done":
            return "+%s" % format_duration(step.after_seconds)
        if run.phase == "holding":
            return "holding — %s" % (run.hold_status or "not ready")
        if run.phase == "sending":
            return "sending…"
        return "next in %s" % format_duration(run.seconds_until_next())


def summary(steps, results):
    """The final tally, plus a line per pane that did not get its prompt."""
    counts = {}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1

    tally = ", ".join(
        "%d %s" % (counts[outcome], label)
        for outcome, label in TALLY_LABELS
        if counts.get(outcome)
    ) or "nothing to do"

    lines = ["[herdr-stagger] %s." % tally]
    for step, result in zip(steps, results):
        if result.outcome in (SENT, PENDING):
            continue
        detail = " — %s" % result.error if result.error else ""
        lines.append(
            "  %-9s %s  %s%s"
            % (result.outcome, step.pane_id, _trim(step.title), detail)
        )
    return "\n".join(lines)
