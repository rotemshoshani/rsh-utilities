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

    def __init__(self, agents=None):
        self.agents = agents if agents is not None else [
            {"pane_id": "wT:p1", "agent": "stubagent", "agent_status": "idle",
             "cwd": os.getcwd(), "foreground_cwd": os.getcwd()},
        ]

    def agent_list(self):
        return self.agents

    def pane_ids(self, workspace_id=None):
        return [a["pane_id"] for a in self.agents]

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

    def test_then_alone_runs_immediately(self):
        """`--then 'gh run watch'` needs no delay; the command blocks itself."""
        state = cli.parse_args(["--then", "gh run watch --exit-status"])
        self.assertEqual(state["mode"], "then")
        self.assertEqual(state["in_seconds"], 0)

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

    def test_a_missing_pane_id_is_resolved_rather_than_fatal(self):
        os.environ["HERDR_ENV"] = "1"
        os.environ.pop("HERDR_PANE_ID", None)
        pane, how = cli.resolve_pane(FakeHerdr())
        self.assertEqual((pane, how), ("wT:p1", "cwd"))


class ResolvePaneTest(unittest.TestCase):
    """Codex runs shell commands under a shared app-server whose environment
    can name a pane that no longer exists, so the variable is a hint."""

    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_trusts_the_env_var_when_it_hosts_an_agent(self):
        os.environ["HERDR_PANE_ID"] = "wT:p1"
        pane, how = cli.resolve_pane(FakeHerdr())
        self.assertEqual((pane, how), ("wT:p1", "environment"))

    def test_falls_back_when_the_env_pane_is_dead(self):
        os.environ["HERDR_PANE_ID"] = "wN:pB"
        hd = FakeHerdr([
            {"pane_id": "wT:p7", "agent": "codex", "agent_status": "working",
             "cwd": os.getcwd(), "foreground_cwd": os.getcwd()},
            {"pane_id": "wT:p8", "agent": "codex", "agent_status": "idle",
             "cwd": "/somewhere/else", "foreground_cwd": "/somewhere/else"},
        ])
        pane, how = cli.resolve_pane(hd)
        self.assertEqual((pane, how), ("wT:p7", "cwd+working"))

    def test_agent_kind_from_ancestry_breaks_a_cwd_tie(self):
        """Several agents share a repo; only one is the kind that called us."""
        os.environ["HERDR_PANE_ID"] = "wN:pB"
        here = os.getcwd()
        kind = cli.caller_agent_kind([{"agent": "python3"}])
        hd = FakeHerdr([
            {"pane_id": "wT:p7", "agent": kind, "agent_status": "working",
             "cwd": here, "foreground_cwd": here},
            {"pane_id": "wT:p8", "agent": "someotheragent", "agent_status": "working",
             "cwd": here, "foreground_cwd": here},
        ])
        pane, how = cli.resolve_pane(hd)
        self.assertEqual(pane, "wT:p7")
        self.assertTrue(how.startswith("kind"), how)

    def test_reads_its_own_kind_off_the_process_tree(self):
        self.assertEqual(cli.caller_agent_kind([{"agent": "python3"}]), "python3")
        self.assertIsNone(cli.caller_agent_kind([{"agent": "nosuchbinary"}]))

    def test_refuses_to_guess_between_equals(self):
        os.environ["HERDR_PANE_ID"] = "wN:pB"
        hd = FakeHerdr([
            {"pane_id": "wT:p7", "agent": "codex", "agent_status": "working",
             "cwd": os.getcwd(), "foreground_cwd": os.getcwd()},
            {"pane_id": "wT:p8", "agent": "codex", "agent_status": "working",
             "cwd": os.getcwd(), "foreground_cwd": os.getcwd()},
        ])
        with self.assertRaises(EnvironmentError) as caught:
            cli.resolve_pane(hd)
        self.assertIn("--pane", str(caught.exception))

    def test_explicit_pane_must_host_an_agent(self):
        with self.assertRaises(EnvironmentError):
            cli.resolve_pane(FakeHerdr(), explicit="wT:p99")


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
