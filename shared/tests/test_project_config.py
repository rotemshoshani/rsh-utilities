from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
READER = ROOT / "shared" / "project_config.py"
EXAMPLE = ROOT / ".rsh-utils.example.json"


class ProjectConfigTests(unittest.TestCase):
    def read(self, data: object, tool: str = "dev_env") -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".rsh-utils.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return subprocess.run(["python3", str(READER), str(path), tool], capture_output=True, check=False)

    def test_example_is_valid_for_both_tools(self) -> None:
        for tool in ("dev_env", "council", "prod_env"):
            result = subprocess.run(["python3", str(READER), str(EXAMPLE), tool], capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_partial_config_emits_only_selected_values(self) -> None:
        result = self.read({"version": 1, "dev_env": {"mode": "sync", "restart_hours": None}})
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.split(b"\0")[:-1], [b"mode", b"sync", b"restart_hours", b"off"])

    def test_rejects_invalid_config(self) -> None:
        result = self.read({"version": 1, "dev_env": {"interval_seconds": 4}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"interval_seconds", result.stderr)

    def test_uat_port(self) -> None:
        for value in (4319, None):
            self.assertEqual(self.read({'version': 1, 'dev_env': {'uat_port': value}}).returncode, 0)
        for value in (80, 65536, True, '4319'):
            self.assertNotEqual(self.read({'version': 1, 'dev_env': {'uat_port': value}}).returncode, 0)

    def test_rejects_unknown_field_and_version(self) -> None:
        unknown = self.read({"version": 1, "council": {"unknown": True}}, "council")
        version = self.read({"version": 2})
        self.assertNotEqual(unknown.returncode, 0)
        self.assertNotEqual(version.returncode, 0)

    def test_prod_env_applies_defaults(self) -> None:
        result = self.read({
            "version": 1,
            "prod_env": {
                "panes": [
                    {"type": "convex", "title": "Errors", "deployment": "prod"},
                    {"type": "vercel", "title": "Deploys", "project": "example"},
                    {"type": "command", "title": "Other", "command": "tail-service"},
                ]
            },
        }, "prod_env")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        fields = result.stdout.split(b"\0")[:-1]
        self.assertEqual(fields[0], b"json")
        parsed = json.loads(fields[1])
        self.assertEqual(parsed["panes"][0]["log_mode"], "errors")
        self.assertEqual(parsed["panes"][0]["history"], 500)
        self.assertEqual(parsed["panes"][1]["target"], "production")
        self.assertEqual(parsed["panes"][1]["poll_seconds"], 30)

    def test_prod_env_rejects_invalid_panes(self) -> None:
        empty = self.read({"version": 1, "prod_env": {"panes": []}}, "prod_env")
        duplicate = self.read({
            "version": 1,
            "prod_env": {"panes": [
                {"type": "command", "title": "Same", "command": "one"},
                {"type": "command", "title": "Same", "command": "two"},
            ]},
        }, "prod_env")
        bad_mode = self.read({
            "version": 1,
            "prod_env": {"panes": [
                {"type": "convex", "title": "Logs", "deployment": "prod", "log_mode": "quiet"},
            ]},
        }, "prod_env")
        self.assertNotEqual(empty.returncode, 0)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertNotEqual(bad_mode.returncode, 0)


if __name__ == "__main__":
    unittest.main()
