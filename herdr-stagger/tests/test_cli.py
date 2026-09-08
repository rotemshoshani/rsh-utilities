import io
import os
import unittest

from herdr_stagger import cli

from stubenv import StubHerdr

AGENTS = [
    {"pane_id": "wQ:p1", "tab_id": "wQ:t1", "workspace_id": "wQ", "agent": "codex",
     "agent_status": "idle", "terminal_title_stripped": "First plan"},
    {"pane_id": "wQ:p2", "tab_id": "wQ:t2", "workspace_id": "wQ", "agent": "codex",
     "agent_status": "idle", "terminal_title_stripped": "Second plan"},
]


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.stub = StubHerdr(self)
        self.stub.agents(AGENTS)
        for agent in AGENTS:
            self.stub.status(agent["pane_id"], "idle")
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run_cli(self, argv, stdin=""):
        return cli.main(
            argv, out=self.out, in_stream=io.StringIO(stdin), err=self.err
        )


class EnvironmentGateTest(CliTestCase):
    def test_refuses_to_run_outside_herdr(self):
        os.environ["HERDR_ENV"] = "0"
        self.assertEqual(self.run_cli(["--yes"]), cli.EXIT_ENV)


class FlagRunTest(CliTestCase):
    def test_flags_alone_need_no_questions(self):
        code = self.run_cli([
            "--pane", "wQ:p1", "--pane", "wQ:p2",
            "--start", "0", "--gap", "0", "--prompt", "go ahead", "--yes",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(
            self.stub.prompts(), [("wQ:p1", "go ahead"), ("wQ:p2", "go ahead")]
        )

    def test_fire_order_follows_the_flag_order(self):
        self.run_cli([
            "--pane", "wQ:p2", "--pane", "wQ:p1",
            "--start", "0", "--gap", "0", "--yes",
        ])
        self.assertEqual([p for p, _ in self.stub.prompts()], ["wQ:p2", "wQ:p1"])

    def test_rejects_a_pane_with_no_live_agent(self):
        code = self.run_cli(["--pane", "wQ:p9", "--start", "0", "--yes"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("wQ:p9", self.err.getvalue())
        self.assertEqual(self.stub.prompts(), [])

    def test_rejects_a_gap_count_that_does_not_fit_the_panes(self):
        code = self.run_cli([
            "--pane", "wQ:p1", "--pane", "wQ:p2",
            "--start", "0", "--gap", "20m", "--gap", "30m", "--yes",
        ])
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_a_stalled_prompt_exits_nonzero(self):
        self.stub.prompt_error("wQ:p1", "agent_prompt_stalled")
        code = self.run_cli(["--pane", "wQ:p1", "--start", "0", "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("agent_prompt_stalled", self.out.getvalue())

    def test_exits_one_when_no_agent_is_running_anywhere(self):
        self.stub.agents([])
        self.assertEqual(self.run_cli(["--yes"]), 1)


class ConfirmationTest(CliTestCase):
    def test_yes_answers_every_question_with_its_default(self):
        """--yes means "ask me nothing", not just "skip the confirmation"."""
        self.run_cli(["--pane", "wQ:p1", "--start", "0", "--yes"])
        self.assertEqual(self.stub.prompts(), [("wQ:p1", cli.DEFAULT_PROMPT)])

    def test_yes_without_panes_is_a_usage_error(self):
        self.assertEqual(self.run_cli(["--yes", "--start", "0"]), cli.EXIT_USAGE)

    def test_declining_sends_nothing(self):
        code = self.run_cli(
            ["--pane", "wQ:p1", "--start", "0", "--prompt", "go"], stdin="n\n"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.prompts(), [])
        self.assertIn("aborted", self.out.getvalue())

    def test_the_plan_is_shown_before_the_question(self):
        self.run_cli(
            ["--pane", "wQ:p1", "--start", "5m", "--prompt", "go"], stdin="n\n"
        )
        self.assertIn("t+5m00s", self.out.getvalue())


class InteractiveTest(CliTestCase):
    def test_picks_panes_by_row_number_and_defaults_the_prompt(self):
        code = self.run_cli([], stdin="1\n0\n\ny\n")
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.prompts(), [("wQ:p1", cli.DEFAULT_PROMPT)])

    def test_asks_for_gaps_only_when_there_is_more_than_one_pane(self):
        self.run_cli([], stdin="1 2\n0\n0\n\ny\n")
        self.assertEqual([p for p, _ in self.stub.prompts()], ["wQ:p1", "wQ:p2"])

    def test_a_bad_answer_is_asked_again_rather_than_fatal(self):
        code = self.run_cli([], stdin="9\n1\n0\n\ny\n")
        self.assertEqual(code, 0)
        self.assertEqual([p for p, _ in self.stub.prompts()], ["wQ:p1"])

    def test_the_candidate_table_is_shown_before_the_question(self):
        self.run_cli([], stdin="1\n0\n\ny\n")
        self.assertIn("First plan", self.out.getvalue())

    def test_a_blank_answer_to_the_confirmation_means_no(self):
        self.run_cli([], stdin="1\n0\n\n\n")
        self.assertEqual(self.stub.prompts(), [])

    def test_an_interrupt_while_answering_aborts_cleanly(self):
        class Interrupting:
            def readline(self):
                raise KeyboardInterrupt

        code = cli.main(
            [], out=self.out, in_stream=Interrupting(), err=self.err
        )
        self.assertEqual(code, 0)
        self.assertIn("aborted", self.out.getvalue())
        self.assertEqual(self.stub.prompts(), [])

    def test_end_of_input_aborts_without_sending(self):
        code = self.run_cli([], stdin="")
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.prompts(), [])


class NotificationTest(CliTestCase):
    def test_a_finished_run_notifies_the_desktop(self):
        """A three-hour run ends when the operator is somewhere else."""
        self.run_cli(["--pane", "wQ:p1", "--start", "0", "--yes"])
        self.assertTrue(
            any(call.startswith("notification show") for call in self.stub.calls())
        )

    def test_no_notify_keeps_it_quiet(self):
        self.run_cli(["--pane", "wQ:p1", "--start", "0", "--yes", "--no-notify"])
        self.assertFalse(
            any(call.startswith("notification show") for call in self.stub.calls())
        )


class SummaryTest(CliTestCase):
    def test_prints_the_tally_when_the_run_ends(self):
        self.run_cli(["--pane", "wQ:p1", "--start", "0", "--yes"])
        self.assertIn("1 sent", self.out.getvalue())
