import io
import unittest

from herdr_stagger import display, picker, runner


class FakeRunner:
    """Just the state the display reads off a live run."""

    def __init__(self, steps, results, index=0, phase="waiting", remaining=0,
                 hold_status=None, started_at=1757340000.0):
        self.steps = steps
        self.results = results
        self.index = index
        self.phase = phase
        self.hold_status = hold_status
        self.started_at = started_at
        self._remaining = remaining

    def seconds_until_next(self):
        return self._remaining


def steps():
    rows = [
        {"pane_id": "wQ:p5Z", "title": "Execute CRM ownership cutover", "kind": "codex"},
        {"pane_id": "wQ:p64", "title": "Resume Priority 2 recovery", "kind": "codex"},
        {"pane_id": "wQ:p6M", "title": "Document template tags guide", "kind": "codex"},
    ]
    return picker.build_plan(rows, 300, [1200, 1200], "go ahead")


class CandidateTableTest(unittest.TestCase):
    def test_numbers_rows_from_one_and_shows_status_and_title(self):
        rows = picker.candidates([
            {"pane_id": "wQ:p64", "agent": "codex", "agent_status": "idle",
             "terminal_title_stripped": "Resume Priority 2 recovery"},
        ])
        text = display.candidate_table(rows)
        self.assertIn("1", text.splitlines()[-1])
        self.assertIn("idle", text)
        self.assertIn("Resume Priority 2 recovery", text)

    def test_marks_the_calling_pane_so_it_is_not_picked_by_accident(self):
        rows = picker.candidates(
            [{"pane_id": "wN:pV", "agent": "claude", "agent_status": "working"}],
            self_pane="wN:pV",
        )
        self.assertIn("this pane", display.candidate_table(rows))


class PlanTableTest(unittest.TestCase):
    def test_shows_cumulative_times_from_launch(self):
        text = display.plan_table(steps())
        self.assertIn("t+5m00s", text)
        self.assertIn("t+25m00s", text)
        self.assertIn("t+45m00s", text)

    def test_says_the_times_are_the_earliest_ones(self):
        """A hold pushes everything back; the table must not read as a promise."""
        self.assertIn("earliest", display.plan_table(steps()).lower())


class SummaryTest(unittest.TestCase):
    def test_counts_every_outcome(self):
        results = [
            runner.Result(runner.SENT, 1757340300.0),
            runner.Result(runner.GAVE_UP, 1757341500.0, 1800, "still blocked after 1800s"),
            runner.Result(runner.CANCELLED),
        ]
        text = display.summary(steps(), results)
        self.assertIn("1 sent", text)
        self.assertIn("1 gave up", text)
        self.assertIn("1 cancelled", text)

    def test_names_the_panes_that_did_not_get_their_prompt(self):
        results = [
            runner.Result(runner.SENT, 1757340300.0),
            runner.Result(runner.FAILED, 1757341500.0, 0, "agent_prompt_stalled: stub"),
            runner.Result(runner.CANCELLED),
        ]
        text = display.summary(steps(), results)
        self.assertIn("wQ:p64", text)
        self.assertIn("Resume Priority 2 recovery — agent_prompt_stalled", text)


class LiveDisplayTest(unittest.TestCase):
    def test_a_plain_stream_gets_one_line_per_event_and_no_escapes(self):
        out = io.StringIO()
        live = display.Display(steps(), out, tty=False)
        state = FakeRunner(steps(), [runner.Result() for _ in range(3)])

        live(state)
        state.results[0] = runner.Result(runner.SENT, 1757340300.0)
        live(state)
        live(state)

        text = out.getvalue()
        self.assertEqual(text.count("wQ:p5Z"), 1)
        self.assertIn("SENT", text)
        self.assertNotIn("\x1b[", text)

    def test_a_terminal_gets_a_countdown_that_redraws_in_place(self):
        out = io.StringIO()
        live = display.Display(steps(), out, tty=True)
        state = FakeRunner(steps(), [runner.Result() for _ in range(3)], remaining=758)

        live(state)
        first = out.getvalue()
        live(state)
        second = out.getvalue()[len(first):]

        self.assertIn("12m38s", first)
        self.assertIn("\x1b[", second)

    def test_a_terminal_shows_what_a_held_pane_is_waiting_on(self):
        out = io.StringIO()
        live = display.Display(steps(), out, tty=True)
        state = FakeRunner(
            steps(), [runner.Result(runner.SENT, 1757340300.0)] + [runner.Result()] * 2,
            index=1, phase="holding", hold_status="blocked",
        )
        live(state)
        self.assertIn("blocked", out.getvalue())
