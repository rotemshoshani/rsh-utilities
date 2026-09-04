"""Delivery tests.

These encode the three findings from the live probe recorded in the design doc:
prompting a blocked agent is destructive, a success response is not evidence of
delivery, and pid is the only identity check that works for every agent kind.
"""

import unittest

from herdr_delay import waiter


class HerdrError(Exception):
    def __init__(self, code, message=""):
        super().__init__(code)
        self.code = code
        self.message = message


class FakeHerdr:
    """Records every call so tests can assert ordering, not just outcomes."""

    HerdrError = HerdrError

    def __init__(self, pids=(4242,), statuses=("idle",), lands=True):
        self.pids = list(pids)
        self.statuses = list(statuses)
        self.lands = lands
        self.calls = []
        self.prompts = []
        self.notifications = []
        self._screen = ""

    def foreground_pids(self, pane_id):
        self.calls.append("foreground_pids")
        return list(self.pids)

    def agent_status(self, pane_id):
        self.calls.append("agent_status")
        return self.statuses[0] if len(self.statuses) == 1 else self.statuses.pop(0)

    def agent_wait_unblocked(self, pane_id, timeout_ms=None):
        self.calls.append("agent_wait_unblocked")
        return {}

    def agent_prompt(self, pane_id, text):
        self.calls.append("agent_prompt")
        self.prompts.append(text)
        if self.lands:
            self._screen += text
        return {}

    def agent_read(self, pane_id, source="detection", lines=200):
        self.calls.append("agent_read")
        return self._screen

    def notification_show(self, title, body=None):
        self.calls.append("notification_show")
        self.notifications.append((title, body))
        return {}


STATE = {
    "pane_id": "wN:pK", "agent_pid": 4242, "marker": "a3f9",
    "mode": "in", "command": None, "reason": "check CI",
    "in_seconds": 600, "every_seconds": 0, "deadline_seconds": 0,
}
RESULT = {"outcome": "RESOLVED", "exit_code": None, "elapsed_seconds": 600,
          "polls": 0, "output": ""}


def deliver(hd):
    """Fake clock advances only when the code sleeps, so retries cost no time."""
    now = [0.0]

    def sleeper(seconds):
        now[0] += seconds

    return waiter.deliver(
        STATE, RESULT, hd=hd, sleeper=sleeper, clock=lambda: now[0],
        log=lambda *a: None,
    )


class HappyPathTest(unittest.TestCase):
    def test_prompts_an_idle_agent_and_verifies(self):
        hd = FakeHerdr()
        self.assertTrue(deliver(hd))
        self.assertEqual(len(hd.prompts), 1)
        self.assertIn("[herdr-delay:a3f9]", hd.prompts[0])

    def test_working_agent_is_steered_without_waiting(self):
        hd = FakeHerdr(statuses=["working"])
        self.assertTrue(deliver(hd))
        self.assertNotIn("agent_wait_unblocked", hd.calls)


class BlockedAgentTest(unittest.TestCase):
    def test_waits_before_prompting_a_blocked_agent(self):
        hd = FakeHerdr(statuses=["blocked", "idle"])
        self.assertTrue(deliver(hd))
        self.assertIn("agent_wait_unblocked", hd.calls)
        self.assertLess(
            hd.calls.index("agent_wait_unblocked"),
            hd.calls.index("agent_prompt"),
            "prompting a blocked agent answers its overlay and discards the text",
        )

    def test_agent_replaced_while_blocked_is_not_prompted(self):
        hd = FakeHerdr(statuses=["blocked", "idle"])

        original = hd.agent_wait_unblocked

        def swap(pane_id, timeout_ms=None):
            hd.pids = [9999]
            return original(pane_id, timeout_ms)

        hd.agent_wait_unblocked = swap
        self.assertFalse(deliver(hd))
        self.assertEqual(hd.prompts, [])


class IdentityTest(unittest.TestCase):
    def test_different_pid_means_a_different_conversation(self):
        hd = FakeHerdr(pids=[9999])
        self.assertFalse(deliver(hd))
        self.assertEqual(hd.prompts, [])
        self.assertEqual(len(hd.notifications), 1)

    def test_no_agent_in_the_pane_is_never_written_to(self):
        hd = FakeHerdr(statuses=[None])
        self.assertFalse(deliver(hd))
        self.assertEqual(hd.prompts, [])


class VerificationTest(unittest.TestCase):
    def test_a_dropped_prompt_is_retried(self):
        hd = FakeHerdr(lands=False)
        self.assertFalse(deliver(hd))
        self.assertEqual(len(hd.prompts), 2, "success responses are not delivery")
        self.assertEqual(len(hd.notifications), 1)

    def test_gone_agent_stops_retrying_immediately(self):
        hd = FakeHerdr()

        def boom(pane_id, text):
            hd.calls.append("agent_prompt")
            raise HerdrError("agent_not_found")

        hd.agent_prompt = boom
        self.assertFalse(deliver(hd))
        self.assertEqual(hd.calls.count("agent_prompt"), 1)


class PaneSurfaceTest(unittest.TestCase):
    def test_never_falls_back_to_the_raw_pane_surface(self):
        """pane run appends Enter, so a fallback would execute the wake text."""
        for hd in (FakeHerdr(pids=[1]), FakeHerdr(statuses=[None]), FakeHerdr(lands=False)):
            deliver(hd)
            for call in hd.calls:
                self.assertFalse(call.startswith("pane_send"), call)


if __name__ == "__main__":
    unittest.main()
