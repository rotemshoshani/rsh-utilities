import unittest

from herdr_stagger import runner
from herdr_stagger.picker import Step


class FakeClock:
    """Time only moves when something sleeps, so tests run instantly."""

    def __init__(self, start=0.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeHerdr:
    """A status per pane: a constant, a callable of the clock, or None (gone)."""

    class HerdrError(Exception):
        def __init__(self, code, message=""):
            super().__init__("%s: %s" % (code, message))
            self.code = code
            self.message = message

    def __init__(self, clock, plan, prompt_error=None):
        self.clock = clock
        self.plan = plan
        self.prompt_error = prompt_error or {}
        self.prompts = []
        self.status_calls = []

    def agent_status(self, pane_id):
        self.status_calls.append((pane_id, self.clock.now))
        value = self.plan.get(pane_id)
        return value(self.clock.now) if callable(value) else value

    def agent_prompt(self, pane_id, text):
        code = self.prompt_error.get(pane_id)
        if code:
            raise self.HerdrError(code, "stub")
        self.prompts.append((pane_id, text, self.clock.now))
        return {}


def steps(*specs, prompt="go ahead"):
    return [
        Step(pane_id=pane, title=pane, kind="codex", after_seconds=after, prompt=prompt)
        for pane, after in specs
    ]


def build(plan, step_list, **kwargs):
    clock = FakeClock()
    hd = FakeHerdr(clock, plan)
    for key in ("prompt_error",):
        if key in kwargs:
            hd.prompt_error = kwargs.pop(key)
    run = runner.Runner(
        step_list, hd, clock=clock.time, sleeper=clock.sleep, **kwargs
    )
    return run, hd, clock


class ScheduleTest(unittest.TestCase):
    def test_sends_each_step_after_its_own_delay(self):
        run, hd, _ = build(
            {"p1": "idle", "p2": "idle"}, steps(("p1", 300), ("p2", 1200))
        )
        run.run()
        self.assertEqual(
            [(p, t) for p, _, t in hd.prompts], [("p1", 300), ("p2", 1500)]
        )

    def test_delivers_the_prompt_text_verbatim(self):
        run, hd, _ = build({"p1": "idle"}, steps(("p1", 60), prompt="implement it"))
        run.run()
        self.assertEqual(hd.prompts, [("p1", "implement it", 60)])

    def test_a_hold_shifts_every_later_step_by_the_hold(self):
        """The gap between implementations is the thing the operator chose."""
        plan = {
            "p1": "idle",
            "p2": lambda now: "blocked" if now < 1860 else "idle",
            "p3": "idle",
        }
        run, hd, _ = build(plan, steps(("p1", 300), ("p2", 1200), ("p3", 1200)))
        run.run()
        self.assertEqual(
            [(p, t) for p, _, t in hd.prompts],
            [("p1", 300), ("p2", 1860), ("p3", 3060)],
        )

    def test_a_working_agent_is_held_for_too(self):
        plan = {"p1": lambda now: "working" if now < 120 else "done"}
        run, hd, _ = build(plan, steps(("p1", 60)))
        results = run.run()
        self.assertEqual(results[0].outcome, runner.SENT)
        self.assertEqual(results[0].held_seconds, 60)


class ReadinessTest(unittest.TestCase):
    def test_done_is_promptable_because_unwatched_panes_settle_there(self):
        run, hd, _ = build({"p1": "done"}, steps(("p1", 60)))
        results = run.run()
        self.assertEqual(results[0].outcome, runner.SENT)
        self.assertEqual(results[0].held_seconds, 0)

    def test_gives_up_after_the_hold_cap(self):
        run, hd, _ = build(
            {"p1": "blocked"}, steps(("p1", 60)), hold_cap_seconds=600
        )
        results = run.run()
        self.assertEqual(results[0].outcome, runner.GAVE_UP)
        self.assertEqual(hd.prompts, [])

    def test_a_step_that_gave_up_still_chains_the_next_gap(self):
        run, hd, _ = build(
            {"p1": "blocked", "p2": "idle"},
            steps(("p1", 60), ("p2", 300)),
            hold_cap_seconds=600,
        )
        run.run()
        self.assertEqual([(p, t) for p, _, t in hd.prompts], [("p2", 960)])

    def test_a_vanished_pane_fails_immediately_without_holding(self):
        run, hd, clock = build({"p1": None}, steps(("p1", 60)), hold_cap_seconds=600)
        results = run.run()
        self.assertEqual(results[0].outcome, runner.FAILED)
        self.assertEqual(clock.now, 60)

    def test_never_prompts_a_blocked_agent(self):
        """Enter answers the overlay's default; the text is discarded."""
        run, hd, _ = build(
            {"p1": "blocked"}, steps(("p1", 60)), hold_cap_seconds=300
        )
        run.run()
        self.assertEqual(hd.prompts, [])


class DeliveryFailureTest(unittest.TestCase):
    def test_a_stalled_prompt_fails_the_step_and_the_run_continues(self):
        clock = FakeClock()
        hd = FakeHerdr(
            clock,
            {"p1": "idle", "p2": "idle"},
            prompt_error={"p1": "agent_prompt_stalled"},
        )
        run = runner.Runner(
            steps(("p1", 60), ("p2", 300)), hd, clock=clock.time, sleeper=clock.sleep
        )
        results = run.run()
        self.assertEqual(results[0].outcome, runner.FAILED)
        self.assertIn("agent_prompt_stalled", results[0].error)
        self.assertEqual([p for p, _, _ in hd.prompts], ["p2"])


class CancellationTest(unittest.TestCase):
    def test_an_interrupt_cancels_every_unsent_step(self):
        clock = FakeClock()
        hd = FakeHerdr(clock, {"p1": "idle", "p2": "idle", "p3": "idle"})

        def sleeper(seconds):
            clock.sleep(seconds)
            if clock.now > 400:
                raise KeyboardInterrupt

        run = runner.Runner(
            steps(("p1", 60), ("p2", 600), ("p3", 600)),
            hd,
            clock=clock.time,
            sleeper=sleeper,
        )
        results = run.run()
        self.assertEqual(
            [r.outcome for r in results],
            [runner.SENT, runner.CANCELLED, runner.CANCELLED],
        )


class ObserverTest(unittest.TestCase):
    def test_the_observer_sees_the_runner_while_it_waits(self):
        seen = []
        run, hd, _ = build(
            {"p1": "idle"},
            steps(("p1", 10)),
            observer=lambda r: seen.append((r.phase, r.seconds_until_next())),
        )
        run.run()
        self.assertIn(("waiting", 10), seen)
        self.assertIn(("waiting", 1), seen)

    def test_the_observer_sees_holding_with_the_blocking_status(self):
        seen = []
        plan = {"p1": lambda now: "blocked" if now < 40 else "idle"}
        run, hd, _ = build(
            plan,
            steps(("p1", 10)),
            observer=lambda r: seen.append((r.phase, r.hold_status)),
        )
        run.run()
        self.assertIn(("holding", "blocked"), seen)
