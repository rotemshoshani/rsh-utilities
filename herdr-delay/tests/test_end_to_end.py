"""End-to-end against a recording `herdr` stub on PATH.

Exercises front, setsid detachment, resolve and delivery as one flow. The real
Herdr socket is never contacted.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUB = os.path.join(ROOT, "tests", "stub")
WRAPPER = os.path.join(ROOT, "herdr-delay")


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stub_dir = os.path.join(self.tmp.name, "stub")
        os.makedirs(self.stub_dir)
        self.env = dict(os.environ)
        self.env.update({
            "PATH": STUB + os.pathsep + os.environ["PATH"],
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "wT:p9",
            "HERDR_WORKSPACE_ID": "wT",
            "XDG_RUNTIME_DIR": self.tmp.name,
            "HD_STUB_DIR": self.stub_dir,
            "HD_STUB_PID": str(os.getpid()),
            "HD_STUB_STATUS": "idle",
        })

    def tearDown(self):
        subprocess.run([WRAPPER, "cancel"], env=self.env, capture_output=True)
        self.tmp.cleanup()

    def run_tool(self, *args):
        return subprocess.run(
            [WRAPPER] + list(args), env=self.env, capture_output=True, text=True
        )

    def wait_for_prompt(self, timeout=20):
        path = os.path.join(self.stub_dir, "prompts")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(path):
                with open(path) as handle:
                    return handle.read()
            time.sleep(0.2)
        return ""

    def read_calls(self):
        path = os.path.join(self.stub_dir, "calls")
        if not os.path.exists(path):
            return ""
        with open(path) as handle:
            return handle.read()

    def test_front_returns_immediately(self):
        start = time.monotonic()
        proc = self.run_tool("--in", "3s")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(time.monotonic() - start, 2.0, "the front must never block")
        self.assertIn("END YOUR TURN NOW", proc.stdout)

    def test_plain_delay_wakes_the_agent(self):
        self.run_tool("--in", "1s", "--reason", "smoke test")
        prompt = self.wait_for_prompt()
        self.assertIn("[herdr-delay:", prompt)
        self.assertIn("RESOLVED", prompt)
        self.assertIn("smoke test", prompt)

    def test_until_polls_then_wakes_with_the_result(self):
        flag = os.path.join(self.tmp.name, "ready")
        helper = subprocess.Popen(["bash", "-c", "sleep 2; echo done > %s" % flag])
        self.addCleanup(helper.wait)
        self.run_tool("--until", "test -f %s && cat %s" % (flag, flag),
                      "--every", "1s", "--deadline", "30s")
        prompt = self.wait_for_prompt()
        self.assertIn("RESOLVED (exit 0)", prompt)
        self.assertIn("done", prompt)

    def test_then_reports_a_failing_command(self):
        self.run_tool("--in", "1s", "--then", "echo BUILD-BROKE >&2; exit 1")
        prompt = self.wait_for_prompt()
        self.assertIn("FAILED (exit 1)", prompt)
        self.assertIn("BUILD-BROKE", prompt)

    def test_deadline_reports_a_timeout(self):
        self.run_tool("--until", "false", "--every", "1s", "--deadline", "2s")
        prompt = self.wait_for_prompt()
        self.assertIn("TIMED_OUT", prompt)

    def test_second_call_replaces_the_first(self):
        self.run_tool("--in", "60s", "--reason", "first")
        second = self.run_tool("--in", "1s", "--reason", "second")
        self.assertIn("replaced the waiter", second.stdout)
        prompt = self.wait_for_prompt()
        self.assertIn("second", prompt)
        self.assertNotIn("first", prompt)

    def test_status_and_cancel_manage_a_live_waiter(self):
        self.run_tool("--in", "60s")
        status = self.run_tool("status")
        self.assertIn("waiter running", status.stdout)
        cancel = self.run_tool("cancel")
        self.assertIn("cancelled", cancel.stdout)
        self.assertIn("no waiter running", self.run_tool("status").stdout)

    def test_blocked_agent_is_waited_on_before_any_prompt(self):
        self.env["HD_STUB_STATUS"] = "blocked"
        self.run_tool("--in", "1s")
        self.wait_for_prompt()
        calls = self.read_calls().splitlines()
        waits = [i for i, c in enumerate(calls) if c.startswith("agent wait")]
        prompts = [i for i, c in enumerate(calls) if c.startswith("pane run")]
        self.assertTrue(waits, "a blocked agent must be waited on")
        self.assertTrue(prompts)
        self.assertLess(waits[0], prompts[0])

    def test_working_agent_idle_transition_still_delivers(self):
        self.env["HD_STUB_STATUS"] = "working"
        self.run_tool("--in", "1s", "--reason", "busy agent completion")
        prompt = self.wait_for_prompt()
        self.assertIn("busy agent completion", prompt)
        self.assertIn("RESOLVED", prompt)
        calls = self.read_calls()
        self.assertIn("agent wait", calls)
        self.assertIn("pane run", calls)

    def test_agent_replaced_after_scheduling_is_never_written_to(self):
        """Same pane, different process: a wake here lands in the wrong session."""
        self.run_tool("--in", "3s")
        with open(os.path.join(self.stub_dir, "pid"), "w") as handle:
            handle.write("1")
        time.sleep(6)
        calls = self.read_calls()
        self.assertNotIn("agent send", calls)
        self.assertNotIn("pane run", calls)
        self.assertNotIn("pane send-keys", calls)
        self.assertIn("notification show", calls)


if __name__ == "__main__":
    unittest.main()
