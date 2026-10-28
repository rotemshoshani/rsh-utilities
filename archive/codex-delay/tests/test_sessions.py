import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_delay.sessions import find_repo_sessions


class SessionTests(unittest.TestCase):
    def test_finds_named_interactive_sessions_for_exact_workdir(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "repo"
            workdir.mkdir()
            codex_dir = root / ".codex"
            sessions_dir = codex_dir / "sessions" / "2026" / "07" / "20"
            sessions_dir.mkdir(parents=True)

            index_records = [
                {"id": "wanted", "thread_name": "old name", "updated_at": "2026-07-20T08:00:00Z"},
                {"id": "wanted", "thread_name": "ui tweaks", "updated_at": "2026-07-20T09:00:00Z"},
                {"id": "other", "thread_name": "another repo", "updated_at": "2026-07-20T10:00:00Z"},
            ]
            (codex_dir / "session_index.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in index_records)
            )
            self._write_session(sessions_dir / "wanted.jsonl", "wanted", workdir)
            self._write_session(sessions_dir / "other.jsonl", "other", root / "elsewhere")
            self._write_session(sessions_dir / "agent.jsonl", "agent", workdir, thread_source="subagent")

            sessions = find_repo_sessions(workdir, codex_dir)

            self.assertEqual([item.id for item in sessions], ["wanted"])
            self.assertEqual(sessions[0].name, "ui tweaks")
            self.assertEqual(sessions[0].thread_name, "ui tweaks")
            self.assertEqual(sessions[0].title, "")
            self.assertEqual(sessions[0].cli_version, "0.144.6")

    def test_reads_rich_metadata_from_codex_state_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "repo"
            workdir.mkdir()
            codex_dir = root / ".codex"
            codex_dir.mkdir()
            (codex_dir / "session_index.jsonl").write_text(
                json.dumps(
                    {
                        "id": "thread-id",
                        "thread_name": "ui tweaks",
                        "updated_at": "2026-07-20T09:00:00Z",
                    }
                )
                + "\n"
            )
            database = sqlite3.connect(codex_dir / "state_5.sqlite")
            database.execute(
                """CREATE TABLE threads (
                    id TEXT, cwd TEXT, title TEXT, updated_at INTEGER, source TEXT,
                    thread_source TEXT, archived INTEGER, has_user_event INTEGER,
                    cli_version TEXT, git_branch TEXT, model TEXT,
                    reasoning_effort TEXT, tokens_used INTEGER, first_user_message TEXT,
                    updated_at_ms INTEGER
                )"""
            )
            database.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "thread-id", str(workdir), "fix the button layout", 1_784_543_600,
                    "cli", "user", 0, 0, "0.144.6", "master", "gpt-5.6",
                    "high", 12345, "first prompt", 0,
                ),
            )
            database.commit()
            database.close()

            sessions = find_repo_sessions(workdir, codex_dir)

            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].name, "ui tweaks")
            self.assertEqual(sessions[0].thread_name, "ui tweaks")
            self.assertEqual(sessions[0].title, "fix the button layout")
            self.assertEqual(sessions[0].git_branch, "master")
            self.assertEqual(sessions[0].model, "gpt-5.6")
            self.assertEqual(sessions[0].tokens_used, 12345)

    @staticmethod
    def _write_session(path: Path, session_id: str, cwd: Path, thread_source: str = "user") -> None:
        record = {
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "timestamp": "2026-07-20T07:00:00Z",
                "cwd": str(cwd),
                "originator": "codex-tui",
                "source": "cli",
                "thread_source": thread_source,
                "cli_version": "0.144.6",
            },
        }
        path.write_text(json.dumps(record) + "\n")


if __name__ == "__main__":
    unittest.main()
