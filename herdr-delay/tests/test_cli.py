import io
import os
import tempfile
import unittest

from herdr_delay import cli


class FakeHerdr:
    class HerdrError(Exception):
        def __init__(self, code):
            super().__init__(code)
            self.code = code

    def foreground_group_id(self, pane_id):
        return os.getpid()


class ParseArgsTest(unittest.TestCase):
    def test_positional_duration_is_sugar_for_in(self):
        state = cli.parse_args(["10m"])
        self.assertEqual(state["mode"], "in")
        self.assertEqual(state["in_seconds"], 600)

    def test_positional_reason_is_collected(self):
        state = cli.parse_args(["10m", "check", "the", "preflight"])
        self.assertEqual(state["reason"], "check the preflight")

    def test_until_defaults_every_and_deadline(self):
        state = cli.parse_args(["--until", "check-ci"])
        self.assertEqual(state["mode"], "until")
        self.assertEqual(state["every_seconds"], 30)
        self.assertEqual(state["deadline_seconds"], 1800)

    def test_then_needs_no_deadline(self):
        state = cli.parse_args(["--in", "1m", "--then", "npm test"])
        self.assertEqual(state["mode"], "then")
        self.assertEqual(state["deadline_seconds"], 0)

    def test_rejects_both_check_modes(self):
        with self.assertRaises(cli.UsageError):
            cli.parse_args(["--until", "a", "--then", "b"])

    def test_rejects_nothing_to_wait_for(self):
        with self.assertRaises(cli.UsageError):
            cli.parse_args([])

    def test_rejects_poll_options_without_until(self):
        with self.assertRaises(cli.UsageError):
            cli.parse_args(["--in", "1m", "--every", "10s"])

    def test_rejects_duplicate_duration(self):
        with self.assertRaises(cli.UsageError):
            cli.parse_args(["10m", "--in", "5m"])

    def test_rejects_a_bad_duration(self):
        with self.assertRaises(cli.UsageError):
            cli.parse_args(["--in", "soon"])


class EnvironmentGateTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_refuses_outside_herdr_rather_than_blocking(self):
        os.environ.pop("HERDR_ENV", None)
        self.assertEqual(cli.main(["10m"], out=io.StringIO(), hd=FakeHerdr()), cli.EXIT_ENV)

    def test_requires_a_pane_id(self):
        os.environ["HERDR_ENV"] = "1"
        os.environ.pop("HERDR_PANE_ID", None)
        self.assertEqual(cli.main(["10m"], out=io.StringIO(), hd=FakeHerdr()), cli.EXIT_ENV)


class StatusAndCancelTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update({
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "wT:p1",
            "XDG_RUNTIME_DIR": self.tmp.name,
        })

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def test_status_reports_nothing_running(self):
        out = io.StringIO()
        self.assertEqual(cli.main(["status"], out=out, hd=FakeHerdr()), 0)
        self.assertIn("no waiter running", out.getvalue())

    def test_cancel_is_harmless_when_idle(self):
        out = io.StringIO()
        self.assertEqual(cli.main(["cancel"], out=out, hd=FakeHerdr()), 0)
        self.assertIn("no waiter running", out.getvalue())

    def test_subcommands_take_no_arguments(self):
        self.assertEqual(cli.main(["cancel", "now"], out=io.StringIO(), hd=FakeHerdr()), cli.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
