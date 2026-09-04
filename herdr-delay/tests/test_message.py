import time
import unittest

from herdr_delay import message


def a_state(**over):
    state = {
        "mode": "until",
        "pane_id": "wN:pK",
        "command": "check-ci",
        "reason": None,
        "in_seconds": 0,
        "every_seconds": 60,
        "deadline_seconds": 1800,
        "started_unix": time.time(),
        "marker": "a3f9",
    }
    state.update(over)
    return state


class StartBannerTest(unittest.TestCase):
    def test_tells_the_agent_to_stop(self):
        banner = message.start_banner(a_state(), 41022)
        self.assertIn("END YOUR TURN NOW", banner)
        self.assertIn("Do not poll, sleep, or re-run", banner)
        self.assertIn("41022", banner)

    def test_shows_the_deadline_only_when_polling(self):
        self.assertIn("deadline", message.start_banner(a_state(), 1))
        plain = a_state(mode="in", command=None, in_seconds=600)
        self.assertNotIn("deadline", message.start_banner(plain, 1))


class WakeMessageTest(unittest.TestCase):
    def test_carries_marker_outcome_and_output(self):
        text = message.wake_message(
            a_state(),
            {"outcome": "RESOLVED", "exit_code": 0, "elapsed_seconds": 1452,
             "polls": 25, "output": "completed\n"},
        )
        self.assertIn("[herdr-delay:a3f9]", text)
        self.assertIn("RESOLVED (exit 0) after 24m12s", text)
        self.assertIn("polls      : 25", text)
        self.assertIn("completed", text)

    def test_timeout_says_the_check_never_passed(self):
        text = message.wake_message(
            a_state(),
            {"outcome": "TIMED_OUT", "exit_code": 1, "elapsed_seconds": 1800,
             "polls": 30, "output": "still running"},
        )
        self.assertIn("TIMED_OUT", text)
        self.assertIn("deadline expired", text)

    def test_plain_delay_needs_no_exit_code(self):
        text = message.wake_message(
            a_state(mode="in", command=None, in_seconds=600, reason="check CI"),
            {"outcome": "RESOLVED", "exit_code": None, "elapsed_seconds": 600,
             "polls": 0, "output": ""},
        )
        self.assertIn("RESOLVED after 10m00s", text)
        self.assertNotIn("exit", text)


class TailTest(unittest.TestCase):
    def test_keeps_only_the_last_lines(self):
        out = message.tail("\n".join(str(i) for i in range(100)))
        self.assertEqual(out.splitlines()[0], "80")
        self.assertEqual(len(out.splitlines()), 20)

    def test_bounds_bytes(self):
        self.assertLessEqual(len(message.tail("x" * 100000).encode()), 4100)


if __name__ == "__main__":
    unittest.main()
