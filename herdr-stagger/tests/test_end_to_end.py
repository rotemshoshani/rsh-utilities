"""The launcher script, the package, and the herdr CLI boundary, together.

Everything below runs the real `./herdr-stagger` in a subprocess against the
recording stub. No test contacts the live Herdr socket.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(ROOT, "herdr-stagger")
STUB_DIR = os.path.join(ROOT, "tests", "stub")

AGENTS = [
    {"pane_id": "wQ:p1", "tab_id": "wQ:t1", "workspace_id": "wQ", "agent": "codex",
     "agent_status": "idle", "terminal_title_stripped": "First plan"},
    {"pane_id": "wQ:p2", "tab_id": "wQ:t2", "workspace_id": "wQ", "agent": "codex",
     "agent_status": "idle", "terminal_title_stripped": "Second plan"},
]


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="herdr-stagger-e2e-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        with open(os.path.join(self.dir, "agents.json"), "w") as handle:
            json.dump(AGENTS, handle)
        os.makedirs(os.path.join(self.dir, "status"))
        for agent in AGENTS:
            name = agent["pane_id"].replace(":", "-")
            with open(os.path.join(self.dir, "status", name), "w") as handle:
                handle.write("idle")

    def launch(self, args, stdin="", herdr_env="1"):
        env = dict(os.environ)
        env["PATH"] = STUB_DIR + os.pathsep + env["PATH"]
        env["HS_STUB_DIR"] = self.dir
        env["HERDR_ENV"] = herdr_env
        env.pop("HERDR_PANE_ID", None)
        return subprocess.run(
            [LAUNCHER] + args,
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def prompts(self):
        path = os.path.join(self.dir, "prompts")
        if not os.path.exists(path):
            return []
        with open(path) as handle:
            return [tuple(line.rstrip("\n").split("\t", 1)) for line in handle if line.strip()]

    def test_a_flagged_run_releases_both_panes_in_order(self):
        proc = self.launch([
            "--pane", "wQ:p2", "--pane", "wQ:p1",
            "--start", "0", "--gap", "0", "--prompt", "implement it", "--yes",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.prompts(), [("wQ:p2", "implement it"), ("wQ:p1", "implement it")]
        )
        self.assertIn("2 sent", proc.stdout)

    def test_an_interactive_run_picks_from_the_table(self):
        proc = self.launch([], stdin="2\n0\n\ny\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Second plan", proc.stdout)
        self.assertEqual([p for p, _ in self.prompts()], ["wQ:p2"])

    def test_a_piped_run_writes_plain_lines_not_cursor_motion(self):
        proc = self.launch([
            "--pane", "wQ:p1", "--start", "0", "--prompt", "go", "--yes",
        ])
        self.assertNotIn("\x1b[", proc.stdout)

    def test_it_refuses_to_run_outside_herdr(self):
        proc = self.launch(["--yes"], herdr_env="")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("HERDR_ENV", proc.stderr)

    def test_it_never_touches_the_raw_pane_surface(self):
        """`pane run` appends Enter to a shell; the prompt would execute."""
        self.launch([
            "--pane", "wQ:p1", "--start", "0", "--prompt", "rm -rf /tmp/x", "--yes",
        ])
        with open(os.path.join(self.dir, "calls")) as handle:
            calls = handle.read()
        self.assertNotIn("pane run", calls)
        self.assertIn("agent prompt", calls)
