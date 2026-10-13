"""Contract tests for the installed Herdr CLI, including submission guards."""

import subprocess
import unittest
from unittest.mock import patch

from herdr_delay import herdr


class HerdrContractTest(unittest.TestCase):
    def test_pane_run_accepts_current_cli_silent_success(self):
        response = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(herdr.subprocess, "run", return_value=response):
            self.assertEqual(herdr._run(["pane", "run", "p", "wake"],
                                        allow_empty=True), {})
            with self.assertRaises(herdr.HerdrError):
                herdr._run(["agent", "get", "p"])

    def test_allow_empty_does_not_accept_help_or_nonzero_exit(self):
        for response in (subprocess.CompletedProcess([], 0, "usage: help", ""),
                         subprocess.CompletedProcess([], 1, "", "")):
            with patch.object(herdr.subprocess, "run", return_value=response):
                with self.assertRaises(herdr.HerdrError):
                    herdr._run(["pane", "run", "p", "wake"], allow_empty=True)

    def test_help_output_is_not_success_even_with_zero_exit(self):
        response = subprocess.CompletedProcess([], 0, "herdr agent commands:\n", "")
        with patch.object(herdr.subprocess, "run", return_value=response):
            with self.assertRaises(herdr.HerdrError) as raised:
                herdr._run(["agent", "prompt", "missing", "test"])
        self.assertEqual(raised.exception.code, "invalid_response")

    def test_current_wait_syntax(self):
        with patch.object(herdr, "_run", return_value={}) as run:
            herdr.agent_wait_unblocked("p", timeout_ms=100)
        run.assert_called_once_with(["agent", "wait", "p", "--status", "idle", "--timeout", "100"])

    def test_native_paste_submission_is_guarded(self):
        with patch.object(herdr, "_run", return_value={}) as run, \
                patch.object(herdr, "foreground_pids", return_value=[42]) as pids, \
                patch.object(herdr, "agent_status", return_value="idle") as status:
            herdr.agent_prompt("p", "wake\nwith multiple lines", expected_pid=42)
        run.assert_called_once_with(["pane", "run", "p", "wake\nwith multiple lines"],
                                    allow_empty=True)
        pids.assert_called_once_with("p")
        status.assert_called_once_with("p")

    def test_replaced_agent_never_receives_pane_run(self):
        with patch.object(herdr, "_run") as run, \
                patch.object(herdr, "foreground_pids", return_value=[99]):
            with self.assertRaises(herdr.HerdrError):
                herdr.agent_prompt("p", "wake", expected_pid=42)
            run.assert_not_called()

    def test_blocked_unknown_or_missing_agent_never_receives_text(self):
        for status in ("blocked", "working", "unknown", None):
            with self.subTest(status=status), \
                    patch.object(herdr, "_run") as run, \
                    patch.object(herdr, "foreground_pids", return_value=[42]), \
                    patch.object(herdr, "agent_status", return_value=status):
                with self.assertRaises(herdr.HerdrError):
                    herdr.agent_prompt("p", "wake", expected_pid=42)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
