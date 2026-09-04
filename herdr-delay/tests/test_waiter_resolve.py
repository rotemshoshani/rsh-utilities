import subprocess
import unittest

from herdr_delay import waiter
from herdr_delay.message import ERROR, FAILED, RESOLVED, TIMED_OUT


class FakeClock:
    """Deterministic clock; sleeping just advances it."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def a_state(**over):
    state = {
        "mode": "until", "command": "check", "in_seconds": 0,
        "every_seconds": 30, "deadline_seconds": 300, "marker": "aaaa",
    }
    state.update(over)
    return state


class ResolveInTest(unittest.TestCase):
    def test_plain_delay_sleeps_once_and_resolves(self):
        clock = FakeClock()
        result = waiter.resolve(
            a_state(mode="in", command=None, in_seconds=600),
            sleeper=clock.sleep, clock=clock,
        )
        self.assertEqual(clock.slept, [600])
        self.assertEqual(result["outcome"], RESOLVED)
        self.assertEqual(result["elapsed_seconds"], 600)
        self.assertIsNone(result["exit_code"])


class ResolveThenTest(unittest.TestCase):
    def test_runs_once_after_the_delay(self):
        clock = FakeClock()
        calls = []

        def runner(cmd, timeout=None):
            calls.append(cmd)
            return 0, "built\n"

        result = waiter.resolve(
            a_state(mode="then", command="npm run preflight", in_seconds=60),
            runner=runner, sleeper=clock.sleep, clock=clock,
        )
        self.assertEqual(calls, ["npm run preflight"])
        self.assertEqual(result["outcome"], RESOLVED)
        self.assertEqual(result["polls"], 1)

    def test_nonzero_exit_is_a_failure_not_a_retry(self):
        clock = FakeClock()
        calls = []

        def runner(cmd, timeout=None):
            calls.append(cmd)
            return 1, "FAIL src/auth.test.ts\n"

        result = waiter.resolve(
            a_state(mode="then", command="npm test", in_seconds=0),
            runner=runner, sleeper=clock.sleep, clock=clock,
        )
        self.assertEqual(len(calls), 1, "--then must never retry")
        self.assertEqual(result["outcome"], FAILED)
        self.assertEqual(result["exit_code"], 1)


class ResolveUntilTest(unittest.TestCase):
    def test_polls_until_exit_zero(self):
        clock = FakeClock()
        codes = iter([1, 1, 1, 0])

        def runner(cmd, timeout=None):
            return next(codes), "probe\n"

        result = waiter.resolve(
            a_state(every_seconds=30, deadline_seconds=600),
            runner=runner, sleeper=clock.sleep, clock=clock,
        )
        self.assertEqual(result["outcome"], RESOLVED)
        self.assertEqual(result["polls"], 4)
        self.assertEqual(clock.slept, [30, 30, 30])

    def test_gives_up_at_the_deadline(self):
        clock = FakeClock()

        def runner(cmd, timeout=None):
            return 1, "not ready"

        result = waiter.resolve(
            a_state(every_seconds=60, deadline_seconds=300),
            runner=runner, sleeper=clock.sleep, clock=clock,
        )
        self.assertEqual(result["outcome"], TIMED_OUT)
        self.assertEqual(result["polls"], 5)
        self.assertLessEqual(sum(clock.slept), 300)

    def test_a_hung_check_is_bounded_by_the_deadline(self):
        clock = FakeClock()

        def runner(cmd, timeout=None):
            raise subprocess.TimeoutExpired("check", timeout)

        result = waiter.resolve(
            a_state(deadline_seconds=120), runner=runner,
            sleeper=clock.sleep, clock=clock,
        )
        self.assertEqual(result["outcome"], TIMED_OUT)

    def test_unrunnable_command_is_an_error(self):
        clock = FakeClock()

        def runner(cmd, timeout=None):
            raise OSError("no bash")

        result = waiter.resolve(
            a_state(), runner=runner, sleeper=clock.sleep, clock=clock
        )
        self.assertEqual(result["outcome"], ERROR)
        self.assertIn("no bash", result["error"])


class RunCheckTest(unittest.TestCase):
    def test_uses_a_shell_so_pipes_work(self):
        code, output = waiter.run_check("printf 'a\\nb\\n' | grep -c b")
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "1")

    def test_combines_stderr(self):
        code, output = waiter.run_check("echo out; echo err >&2; exit 3")
        self.assertEqual(code, 3)
        self.assertIn("out", output)
        self.assertIn("err", output)


if __name__ == "__main__":
    unittest.main()
