import json
import os
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

import prompt_queue
from prompt_queue import (
    Controller,
    RunItem,
    apply_agent_override,
    archive_queue_directory,
    build_prompt_argument_command,
    build_prompt_list_watch_command,
    build_resume_prompt_command,
    build_run_queue,
    build_worker_cd_command,
    consume_finish_current_sleep,
    default_work_base_dir,
    default_config_path,
    format_elapsed,
    format_index_ranges,
    kill_tmux_session_if_exists,
    latest_runtime_dir,
    load_env_file,
    load_config,
    make_runtime_dir,
    paste_settle_seconds,
    queue_config_path,
    read_resumable_completed_indices,
    read_prompt_queue,
    render_prompt_list,
    render_run_preflight,
    render_status,
    prompt_start_new_run,
    RunPlan,
    should_finish_current_sleep,
    should_stop_after_current,
    stop_next,
    toggle_stop_after_current,
    write_collected_prompts,
    write_prompt_queue,
)


class PromptQueueTests(unittest.TestCase):
    def test_load_config_reads_prompts_and_defaults(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            prompt_file = tmp_path / "file-prompt.md"
            prompt_file.write_text("Prompt from file")
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": "/tmp/project",
                        "command": "cdx",
                        "startup_wait_seconds": 12,
                        "ready_check_seconds": 30,
                        "ready_check_lines": 2,
                        "ready_markers": ["Ready", "done"],
                        "block_marker": "DO-NOT-PROCEED",
                        "block_check_lines": 12,
                        "capture_lines": 250,
                        "prompts": [
                            {"name": "inline prompt", "text": "Prompt from config"},
                            {"name": "lines prompt", "lines": ["First line", "Second line"]},
                            {"name": "file prompt", "file": "file-prompt.md"},
                        ],
                    }
                )
            )

            config = load_config(config_path)

            self.assertEqual(config.project_dir, Path("/tmp/project"))
            self.assertEqual(config.command, "cdx")
            self.assertEqual(config.startup_wait_seconds, 12)
            self.assertEqual(config.ready_check_seconds, 30)
            self.assertEqual(config.ready_check_lines, 2)
            self.assertEqual(config.ready_markers, ("Ready", "done"))
            self.assertEqual(config.block_marker, "DO-NOT-PROCEED")
            self.assertEqual(config.block_check_lines, 12)
            self.assertEqual(config.capture_lines, 250)
            self.assertEqual([(item.index, item.name, item.text) for item in config.prompts], [
                (1, "inline-prompt", "Prompt from config"),
                (2, "lines-prompt", "First line\nSecond line"),
                (3, "file-prompt", "Prompt from file"),
            ])

    def test_project_dir_resolves_relative_to_config_file(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            project_dir = tmp_path / "repo"
            project_dir.mkdir()
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps({"project_dir": "repo", "prompts": ["prompt"]}))

            config = load_config(config_path)

            self.assertEqual(config.project_dir, project_dir)

    def test_default_config_prefers_ignored_local_config_when_present(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            default_path = tmp_path / "config.json"
            local_path = tmp_path / "config.local.json"

            with (
                mock.patch.object(prompt_queue, "DEFAULT_CONFIG", default_path),
                mock.patch.object(prompt_queue, "LOCAL_CONFIG", local_path),
            ):
                self.assertEqual(default_config_path(), default_path)
                local_path.write_text("{}")
                self.assertEqual(default_config_path(), local_path)
                args = prompt_queue.build_parser().parse_args(["run", "sample-queue", "--no-attach"])
                self.assertEqual(args.config, str(local_path))

    def test_run_parser_accepts_no_confirm_aliases_after_run(self) -> None:
        parser = prompt_queue.build_parser()

        self.assertTrue(parser.parse_args(["run", "sample-queue", "--no-confirm"]).no_confirm)
        self.assertTrue(parser.parse_args(["run", "sample-queue", "--yes"]).no_confirm)
        self.assertTrue(parser.parse_args(["run", "sample-queue", "-y"]).no_confirm)

    def test_public_commands_require_queue_id(self) -> None:
        parser = prompt_queue.build_parser()

        for command in ("run", "attach", "stop-next", "finish-sleep", "kill", "status", "collect-prompts"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parser.parse_args([command])

    def test_queue_config_path_uses_validated_queue_folder(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            root = Path(raw_dir) / "queues"
            folder = root / "release-42"
            folder.mkdir(parents=True)
            config_path = folder / "config.json"
            config_path.write_text("{}")
            with mock.patch.object(prompt_queue, "QUEUES_DIR", root):
                self.assertEqual(queue_config_path("release-42"), config_path)
                with self.assertRaisesRegex(SystemExit, "may contain only"):
                    queue_config_path("../escape")

    def test_prompts_string_that_points_to_file_raises_clear_error(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            prompts_dir = tmp_path / "prompts"
            prompts_dir.mkdir()
            (prompts_dir / "001-work.md").write_text("actual prompt body")
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": "/tmp/project",
                        "prompts": ["prompts/001-work.md"],
                        "prompt_files": [],
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "Move file path 'prompts/001-work.md' to 'prompt_files'"):
                load_config(config_path)

    def test_project_dir_can_come_from_env_local(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            project_dir = tmp_path / "repo"
            project_dir.mkdir()
            config_path = tmp_path / "config.json"
            env_path = tmp_path / ".env.local"
            old_value = os.environ.get("PROMPT_QUEUE_WORKDIR")
            try:
                env_path.write_text(f"PROMPT_QUEUE_WORKDIR={project_dir}\n")
                config_path.write_text(json.dumps({"project_dir": "${PROMPT_QUEUE_WORKDIR}", "prompts": ["prompt"]}))

                config = load_config(config_path)

                self.assertEqual(config.project_dir, project_dir)
            finally:
                if old_value is None:
                    os.environ.pop("PROMPT_QUEUE_WORKDIR", None)
                else:
                    os.environ["PROMPT_QUEUE_WORKDIR"] = old_value

    def test_load_env_file_parses_export_and_quotes(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            env_path = Path(raw_dir) / ".env.local"
            old_value = os.environ.get("PROMPT_QUEUE_WORKDIR")
            try:
                env_path.write_text("export PROMPT_QUEUE_WORKDIR='/tmp/quoted repo'\n")

                loaded = load_env_file(env_path)

                self.assertEqual(loaded["PROMPT_QUEUE_WORKDIR"], "/tmp/quoted repo")
                self.assertEqual(os.environ["PROMPT_QUEUE_WORKDIR"], "/tmp/quoted repo")
            finally:
                if old_value is None:
                    os.environ.pop("PROMPT_QUEUE_WORKDIR", None)
                else:
                    os.environ["PROMPT_QUEUE_WORKDIR"] = old_value

    def test_prompt_queue_round_trips_to_json(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.json"
            queue_path = Path(raw_dir) / "queue.json"
            config_path.write_text(json.dumps({"prompts": ["one", "two"]}))
            prompts = load_config(config_path).prompts

            write_prompt_queue(queue_path, prompts)

            self.assertEqual(read_prompt_queue(queue_path), prompts)

    def test_write_prompt_queue_includes_hash_manifest(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.json"
            queue_path = Path(raw_dir) / "queue.json"
            config_path.write_text(json.dumps({"prompts": ["one", "two"]}))
            prompts = load_config(config_path).prompts

            write_prompt_queue(queue_path, prompts)

            raw = json.loads(queue_path.read_text())
            self.assertEqual(raw["version"], 1)
            self.assertTrue(str(raw["queue_id"]).startswith("sha256:"))
            self.assertEqual(len(raw["items"]), 2)
            self.assertTrue(str(raw["items"][0]["content_hash"]).startswith("sha256:"))
            self.assertEqual(read_prompt_queue(queue_path), prompts)

    def test_read_prompt_queue_keeps_legacy_list_compatibility(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            queue_path = Path(raw_dir) / "queue.json"
            queue_path.write_text(
                json.dumps(
                    [
                        {"index": 1, "name": "first", "text": "Prompt one", "source": "config"},
                        {"index": 2, "name": "second", "text": "Prompt two", "source": "config"},
                    ]
                )
            )

            prompts = read_prompt_queue(queue_path)

            self.assertEqual([(item.index, item.name, item.text) for item in prompts], [
                (1, "first", "Prompt one"),
                (2, "second", "Prompt two"),
            ])

    def test_write_collected_prompts_writes_files_and_updates_config(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": "/tmp/project",
                        "prompts": [{"name": "old", "text": "old"}],
                        "prompt_files": ["prompts/old.md"],
                    }
                )
            )

            written = write_collected_prompts(
                config_path,
                [("First prompt", "first body"), ("Second prompt", "second body")],
            )

            raw = json.loads(config_path.read_text())
            self.assertEqual(
                raw["prompts"],
                [
                    {"name": "First-prompt", "file": "prompts/001-First-prompt.md"},
                    {"name": "Second-prompt", "file": "prompts/002-Second-prompt.md"},
                ],
            )
            self.assertEqual(raw["prompt_files"], [])
            self.assertEqual([path.name for path in written], ["001-First-prompt.md", "002-Second-prompt.md"])
            self.assertEqual((Path(raw_dir) / "prompts/001-First-prompt.md").read_text(), "first body\n")

    def test_build_run_queue_runs_one_codex_instance_per_prompt(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "command": "cdx",
                        "prompt_delivery": "argument_file",
                        "prompts": [
                            {"name": "first", "text": "Prompt one"},
                            {"name": "second", "text": "Prompt two"},
                        ],
                    }
                )
            )

            queue = build_run_queue(load_config(config_path))

            self.assertEqual(
                [(item.index, item.prompt_name, item.command, item.prompt_delivery) for item in queue],
                [
                    (1, "first", "cdx", "argument_file"),
                    (2, "second", "cdx", "argument_file"),
                ],
            )

    def test_cld_override_reuses_queue_with_claude_command_and_paste_delivery(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "command": "cdx",
                        "prompt_delivery": "argument_file",
                        "prompts": [{"name": "first", "text": "Prompt one"}],
                    }
                )
            )

            config = apply_agent_override(load_config(config_path), use_claude=True)
            queue = build_run_queue(config)

            self.assertEqual(config.command, "cld")
            self.assertEqual(config.prompt_delivery, "paste")
            self.assertEqual([(item.prompt_name, item.command, item.prompt_delivery) for item in queue], [
                ("first", "cld", "paste")
            ])

    def test_agent_override_leaves_default_codex_config_unchanged(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.json"
            config_path.write_text(json.dumps({"command": "cdx", "prompt_delivery": "argument_file", "prompts": ["one"]}))
            config = load_config(config_path)

            self.assertIs(apply_agent_override(config, use_claude=False), config)

    def test_unresolved_recovery_session_env_is_treated_as_missing(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "blocked_recovery": True,
                        "blocked_recovery_session_id": "${PROMPT_QUEUE_MISSING_SESSION}",
                        "prompts": ["one"],
                    }
                )
            )

            config = load_config(config_path)

            self.assertEqual(config.blocked_recovery_session_id, "")

    def test_completion_notify_reuses_recovery_session_by_default(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "completion_notify": True,
                        "blocked_recovery_session_id": "planner-session",
                        "prompts": ["one"],
                    }
                )
            )

            config = load_config(config_path)

            self.assertTrue(config.completion_notify)
            self.assertEqual(config.completion_notify_session_id, "planner-session")

    def test_unresolved_completion_session_env_is_treated_as_missing(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "completion_notify": True,
                        "completion_notify_session_id": "${PROMPT_QUEUE_MISSING_SESSION}",
                        "prompts": ["one"],
                    }
                )
            )

            config = load_config(config_path)

            self.assertEqual(config.completion_notify_session_id, "")

    def test_load_config_reads_relative_completion_prompt_file(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            completion_prompt = tmp_path / "finish.md"
            completion_prompt.write_text("Finish the authorized PR workflow.\n")
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "completion_prompt_file": "finish.md",
                        "prompts": ["one"],
                    }
                )
            )

            config = load_config(config_path)

            self.assertEqual(config.completion_prompt, "Finish the authorized PR workflow.")

    def test_load_config_rejects_missing_completion_prompt_file(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "completion_prompt_file": "missing.md",
                        "prompts": ["one"],
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "completion_prompt_file does not exist"):
                load_config(config_path)

    def test_render_status_shows_sectioned_dashboard(self) -> None:
        queue = [
            RunItem(index=1, prompt_name="first", prompt_text="one", prompt_source="test", command="cdx"),
            RunItem(index=2, prompt_name="second", prompt_text="two", prompt_source="test", command="cdx"),
            RunItem(index=3, prompt_name="third", prompt_text="three", prompt_source="test", command="cdx"),
        ]

        status = render_status(
            queue,
            current_index=2,
            completed={1},
            stop_after_current=True,
            phase="agent working",
            total_elapsed_seconds=3661,
            prompt_elapsed_seconds=65,
            last_check_at="2026-06-29T10:00:00",
            last_check_line="Waiting for Ready",
            prompt_durations={1: 30},
            title="repo",
        )

        self.assertIn("prompt-queue  repo", status)
        self.assertIn("running  1h 1m 1s", status)
        self.assertIn("Progress", status)
        self.assertIn("done     1/3", status)
        self.assertIn("current  002 second", status)
        self.assertIn("left     1", status)
        self.assertNotIn("Queue", status)
        self.assertNotIn("[x] 001 first", status)
        self.assertNotIn("[>] 002 second", status)
        self.assertNotIn("[ ] 003 third", status)
        self.assertIn("Current Prompt", status)
        self.assertIn("phase       agent working", status)
        self.assertIn("elapsed     1m 5s", status)
        self.assertIn("last check  10:00:00", status)
        self.assertIn("last line   Waiting for Ready", status)
        self.assertIn("Controls", status)
        self.assertIn("S stop after current    F finish wait    Q kill now", status)
        self.assertNotIn("ETA", status)

    def test_render_status_shows_total_and_current_prompt_elapsed_time(self) -> None:
        queue = [
            RunItem(index=1, prompt_name="first", prompt_text="one", prompt_source="test", command="cdx"),
            RunItem(index=2, prompt_name="second", prompt_text="two", prompt_source="test", command="cdx"),
        ]

        status = render_status(
            queue,
            current_index=2,
            completed={1},
            stop_after_current=False,
            phase="agent working",
            total_elapsed_seconds=3661,
            prompt_elapsed_seconds=65,
        )

        self.assertIn("running  1h 1m 1s", status)
        self.assertIn("elapsed     1m 5s", status)

    def test_render_prompt_list_compacts_large_queue_around_current_prompt(self) -> None:
        queue = [
            RunItem(index=index, prompt_name=f"prompt-{index}", prompt_text=str(index), prompt_source="test", command="cdx")
            for index in range(1, 15)
        ]

        prompt_list = render_prompt_list(
            queue,
            current_index=6,
            completed={1, 2, 3, 4, 5},
            completed_window=3,
            queued_window=4,
        )

        self.assertIn("Prompt List", prompt_list)
        self.assertIn("... 2 completed prompts hidden", prompt_list)
        self.assertNotIn("[x] 001 prompt-1", prompt_list)
        self.assertIn("[x] 003 prompt-3", prompt_list)
        self.assertIn("[>] 006 prompt-6", prompt_list)
        self.assertIn("[ ] 010 prompt-10", prompt_list)
        self.assertNotIn("[ ] 011 prompt-11", prompt_list)
        self.assertIn("... 4 queued prompts hidden", prompt_list)

    def test_render_prompt_list_shows_only_completed_durations(self) -> None:
        queue = [
            RunItem(index=1, prompt_name="first", prompt_text="one", prompt_source="test", command="cdx"),
            RunItem(index=2, prompt_name="second", prompt_text="two", prompt_source="test", command="cdx"),
        ]

        prompt_list = render_prompt_list(
            queue,
            current_index=2,
            completed={1},
            prompt_durations={1: 30},
            prompt_elapsed_seconds=65,
        )

        self.assertIn("[x] 001 first", prompt_list)
        self.assertIn("30s", prompt_list)
        self.assertIn("[>] 002 second", prompt_list)
        self.assertNotIn("1m 5s", prompt_list)

    def test_format_elapsed_uses_clock_style_for_long_runs(self) -> None:
        self.assertEqual(format_elapsed(0), "0s")
        self.assertEqual(format_elapsed(65), "1m 5s")
        self.assertEqual(format_elapsed(3661), "1h 1m 1s")

    def test_format_index_ranges_compacts_contiguous_indices(self) -> None:
        self.assertEqual(format_index_ranges([]), "none")
        self.assertEqual(format_index_ranges([1, 2, 3, 5, 7, 8]), "1-3,5,7-8")

    def test_render_run_preflight_shows_resume_and_existing_session(self) -> None:
        plan = RunPlan(
            project_dir=Path("/repo"),
            session="prompt-queue-repo",
            prompt_count=23,
            completed=[1, 2, 3, 4, 5],
            pending=list(range(6, 24)),
            resume_runtime_dir=Path("/repo/.planning/work/prompt-queue/20260629-120000"),
            existing_session=True,
        )

        rendered = render_run_preflight(plan)

        self.assertIn("target repo: /repo", rendered)
        self.assertIn("queue: 23 prompts", rendered)
        self.assertIn("resume source: /repo/.planning/work/prompt-queue/20260629-120000", rendered)
        self.assertIn("completed: 1-5", rendered)
        self.assertIn("will run: 6-23", rendered)
        self.assertIn("existing tmux session: prompt-queue-repo", rendered)

    def test_prompt_start_new_run_defaults_to_yes_and_rejects_no(self) -> None:
        plan = RunPlan(
            project_dir=Path("/repo"),
            session="prompt-queue-repo",
            prompt_count=1,
            completed=[],
            pending=[1],
            resume_runtime_dir=None,
            existing_session=False,
        )

        with mock.patch("builtins.input", return_value=""), mock.patch("builtins.print"):
            self.assertTrue(prompt_start_new_run(plan))
        with mock.patch("builtins.input", return_value="n"), mock.patch("builtins.print"):
            self.assertFalse(prompt_start_new_run(plan))

    def test_should_stop_after_current_reads_flag_file(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir) / "runtime"
            runtime_dir.mkdir()

            self.assertFalse(should_stop_after_current(runtime_dir))

            (runtime_dir / "stop-next.flag").write_text("1")

            self.assertTrue(should_stop_after_current(runtime_dir))

    def test_stop_after_current_flag_toggles(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir) / "runtime"
            runtime_dir.mkdir()

            self.assertTrue(toggle_stop_after_current(runtime_dir))
            self.assertTrue(should_stop_after_current(runtime_dir))

            self.assertFalse(toggle_stop_after_current(runtime_dir))
            self.assertFalse(should_stop_after_current(runtime_dir))

    def test_stop_next_command_toggles_latest_runtime_flag(self) -> None:
        from argparse import Namespace
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            project_dir = tmp_path / "repo"
            runtime_dir = project_dir / ".planning/work/prompt-queue/20260629-120000"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "session.json").write_text("{}")
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps({"project_dir": str(project_dir), "prompts": ["prompt"]}))
            args = Namespace(config=str(config_path))

            with mock.patch("builtins.print") as mocked_print:
                stop_next(args)
                stop_next(args)

            self.assertFalse(should_stop_after_current(runtime_dir))
            self.assertIn("armed stop-after-current", mocked_print.call_args_list[0].args[0])
            self.assertIn("disarmed stop-after-current", mocked_print.call_args_list[1].args[0])

    def test_finish_current_sleep_flag_is_consumed(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir) / "runtime"
            runtime_dir.mkdir()

            self.assertFalse(should_finish_current_sleep(runtime_dir))
            self.assertFalse(consume_finish_current_sleep(runtime_dir))

            flag = runtime_dir / "finish-sleep.flag"
            flag.write_text("1")

            self.assertTrue(should_finish_current_sleep(runtime_dir))
            self.assertTrue(consume_finish_current_sleep(runtime_dir))
            self.assertFalse(flag.exists())

    def test_build_worker_cd_command_quotes_project_path(self) -> None:
        command = build_worker_cd_command(Path("/tmp/project with 'quote'"))

        self.assertEqual(command, "cd -- '/tmp/project with '\"'\"'quote'\"'\"''")

    def test_build_prompt_argument_command_reads_prompt_file_as_single_argument(self) -> None:
        command = build_prompt_argument_command("cdx", Path("/tmp/project prompt.txt"))

        self.assertEqual(command, 'cdx "$(cat \'/tmp/project prompt.txt\')"')

    def test_build_resume_prompt_command_quotes_session_and_prompt_file(self) -> None:
        command = build_resume_prompt_command("cdx", "session with 'quote'", Path("/tmp/recovery prompt.txt"))

        self.assertEqual(
            command,
            'cdx resume \'session with \'"\'"\'quote\'"\'"\'\' "$(cat \'/tmp/recovery prompt.txt\')"',
        )

    def test_build_prompt_list_watch_command_refreshes_prompt_list_file(self) -> None:
        command = build_prompt_list_watch_command(Path("/tmp/runtime prompt-list.txt"))

        self.assertIn("while true", command)
        self.assertIn("cat '/tmp/runtime prompt-list.txt'", command)
        self.assertIn("sleep 1", command)

    def test_paste_settle_seconds_scales_by_prompt_size(self) -> None:
        self.assertEqual(paste_settle_seconds(499), 1.5)
        self.assertEqual(paste_settle_seconds(500), 2.5)
        self.assertEqual(paste_settle_seconds(5000), 4.0)
        self.assertEqual(paste_settle_seconds(20000), 6.0)

    def test_paste_prompt_uses_tmux_bracketed_paste_and_sends_enter(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps({"project_dir": "/tmp/project", "prompts": ["prompt"]}))
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = Controller(config, "session", "%1")
            prompt_file = tmp_path / "prompt.txt"
            prompt_file.write_text("hello\n\n")
            calls: list[tuple[tuple[str, ...], str | None]] = []

            def fake_tmux(*args: str, **kwargs: object) -> CompletedProcess[str]:
                calls.append((args, kwargs.get("input_text") if isinstance(kwargs.get("input_text"), str) else None))
                return CompletedProcess(["tmux", *args], 0, "", "")

            with mock.patch.object(prompt_queue, "tmux", side_effect=fake_tmux), mock.patch.object(prompt_queue.time, "sleep"):
                controller.paste_prompt(prompt_file)

            self.assertEqual(calls[0], (("load-buffer", "-b", mock.ANY, "-"), "hello"))
            self.assertEqual(calls[1][0][:6], ("paste-buffer", "-d", "-p", "-b", mock.ANY, "-t"))
            self.assertEqual(calls[1][0][6:], ("%1",))
            self.assertEqual(calls[2], (("send-keys", "-t", "%1", "Enter"), None))

    def test_kill_tmux_session_if_exists_kills_existing_session(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_tmux(*args: str, **kwargs: object) -> CompletedProcess[str]:
            calls.append(args)
            if args[:1] == ("has-session",):
                return CompletedProcess(["tmux", *args], 0, "", "")
            return CompletedProcess(["tmux", *args], 0, "", "")

        with mock.patch.object(prompt_queue, "tmux", side_effect=fake_tmux):
            killed = kill_tmux_session_if_exists("prompt-queue-project")

        self.assertTrue(killed)
        self.assertEqual(calls, [
            ("has-session", "-t", "prompt-queue-project"),
            ("kill-session", "-t", "prompt-queue-project"),
        ])

    def test_kill_tmux_session_if_exists_skips_missing_session(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_tmux(*args: str, **kwargs: object) -> CompletedProcess[str]:
            calls.append(args)
            return CompletedProcess(["tmux", *args], 1, "", "")

        with mock.patch.object(prompt_queue, "tmux", side_effect=fake_tmux):
            killed = kill_tmux_session_if_exists("prompt-queue-project")

        self.assertFalse(killed)
        self.assertEqual(calls, [("has-session", "-t", "prompt-queue-project")])

    def test_sync_tmux_session_environment_refreshes_user_bus_for_new_panes(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_tmux(*args: str, **kwargs: object) -> CompletedProcess[str]:
            calls.append(args)
            return CompletedProcess(["tmux", *args], 0, "", "")

        with (
            mock.patch.dict(
                os.environ,
                {
                    "XDG_RUNTIME_DIR": "/run/user/1234",
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1234/bus",
                },
                clear=True,
            ),
            mock.patch.object(prompt_queue, "tmux", side_effect=fake_tmux),
        ):
            prompt_queue.sync_tmux_session_environment()

        self.assertEqual(calls, [
            ("set-environment", "-g", "XDG_RUNTIME_DIR", "/run/user/1234"),
            ("set-environment", "-g", "DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1234/bus"),
        ])

    def test_controller_process_is_running_detects_stale_pid(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir)
            (runtime_dir / "controller.json").write_text(
                json.dumps({"pid": 424242, "session": "prompt-queue-project"})
            )
            with mock.patch.object(prompt_queue.os, "kill", side_effect=ProcessLookupError):
                running = prompt_queue.controller_process_is_running(
                    runtime_dir,
                    "prompt-queue-project",
                )

        self.assertFalse(running)

    def test_prepare_tmux_session_replacement_renames_current_session(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_tmux(*args: str, **kwargs: object) -> CompletedProcess[str]:
            calls.append(args)
            return CompletedProcess(["tmux", *args], 0, "", "")

        with (
            mock.patch.object(prompt_queue, "current_tmux_session", return_value="prompt-queue-project"),
            mock.patch.object(prompt_queue.os, "getpid", return_value=4321),
            mock.patch.object(prompt_queue, "tmux", side_effect=fake_tmux),
        ):
            stale_session = prompt_queue.prepare_tmux_session_replacement("prompt-queue-project")

        self.assertEqual(stale_session, "prompt-queue-project-stale-4321")
        self.assertEqual(calls, [
            (
                "rename-session",
                "-t",
                "prompt-queue-project",
                "prompt-queue-project-stale-4321",
            )
        ])

    def test_ready_marker_blocks_on_exact_marker_line_only(self) -> None:
        from tempfile import TemporaryDirectory

        class FakeController(Controller):
            def __init__(self, *args: object) -> None:
                super().__init__(*args)
                self.calls = 0

            def capture_worker_tail(self, lines: int) -> str:
                self.calls += 1
                if self.calls == 1:
                    return "gpt status Ready\n"
                return "This mentions DO-NOT-PROCEED but is not exact\nDO-NOT-PROCEED\nReady\n"

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": "/tmp/project",
                        "ready_check_lines": 1,
                        "block_marker": "DO-NOT-PROCEED",
                        "block_check_lines": 10,
                        "prompts": ["prompt"],
                    }
                )
            )
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = FakeController(config, "session", "%1")
            controller.ready_check_dir.mkdir(parents=True)

            result = controller.check_ready_marker(
                RunItem(index=1, prompt_name="prompt-1", prompt_text="prompt", prompt_source="test", command="cdx")
            )

            self.assertEqual(result, "blocked")
            self.assertTrue(controller.block_detected)
            self.assertEqual(controller.block_marker_line, "DO-NOT-PROCEED")

    def test_ready_marker_ignores_non_exact_block_marker_mentions(self) -> None:
        from tempfile import TemporaryDirectory

        class FakeController(Controller):
            def __init__(self, *args: object) -> None:
                super().__init__(*args)
                self.calls = 0

            def capture_worker_tail(self, lines: int) -> str:
                self.calls += 1
                if self.calls == 1:
                    return "gpt status Ready\n"
                return "This mentions DO-NOT-PROCEED but is not exact\nReady\n"

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": "/tmp/project",
                        "ready_check_lines": 1,
                        "block_marker": "DO-NOT-PROCEED",
                        "block_check_lines": 10,
                        "prompts": ["prompt"],
                    }
                )
            )
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = FakeController(config, "session", "%1")
            controller.ready_check_dir.mkdir(parents=True)

            result = controller.check_ready_marker(
                RunItem(index=1, prompt_name="prompt-1", prompt_text="prompt", prompt_source="test", command="cdx")
            )

            self.assertEqual(result, "ready")
            self.assertFalse(controller.block_detected)

    def test_worker_wait_uses_ready_marker_without_timeout(self) -> None:
        from tempfile import TemporaryDirectory

        class FakeController(Controller):
            def __init__(self, *args: object) -> None:
                super().__init__(*args)
                self.calls = 0

            def check_ready_marker(self, item: RunItem) -> str:
                self.calls += 1
                return "ready" if self.calls == 2 else "waiting"

            def handle_keyboard(self) -> None:
                return None

            def render(self) -> None:
                return None

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": "/tmp/project",
                        "ready_check_seconds": 1,
                        "prompts": ["prompt"],
                    }
                )
            )
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = FakeController(config, "session", "%1")

            with (
                mock.patch.object(prompt_queue.time, "sleep"),
                mock.patch.object(prompt_queue.time, "time", side_effect=[0, 0, 1, 1, 2]),
            ):
                result = controller.wait_for_worker_ready(
                    "agent working",
                    RunItem(index=1, prompt_name="prompt-1", prompt_text="prompt", prompt_source="test", command="cdx"),
                )

            self.assertEqual(result, "ready")
            self.assertEqual(controller.calls, 2)

    def test_recovery_marker_waits_for_ready_before_proceeding(self) -> None:
        from tempfile import TemporaryDirectory

        class FakeController(Controller):
            def capture_pane_tail(self, pane: str, lines: int) -> str:
                return "PROCEED-ALLOWED\n"

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": "/tmp/project",
                        "ready_check_lines": 1,
                        "blocked_recovery": True,
                        "blocked_recovery_session_id": "session-id",
                        "prompts": ["prompt"],
                    }
                )
            )
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = FakeController(config, "session", "%1", "%2")
            controller.recovery_check_dir.mkdir(parents=True)

            result = controller.check_recovery_marker(
                RunItem(index=1, prompt_name="prompt-1", prompt_text="prompt", prompt_source="test", command="cdx"),
                recovery_attempts=0,
            )

            self.assertEqual(result, "waiting")

    def test_recovery_marker_proceeds_only_after_ready_bottom_line(self) -> None:
        from tempfile import TemporaryDirectory

        class FakeController(Controller):
            def __init__(self, *args: object) -> None:
                super().__init__(*args)
                self.calls = 0

            def capture_pane_tail(self, pane: str, lines: int) -> str:
                self.calls += 1
                if self.calls == 1:
                    return "Ready\n"
                return "Fixed the issue.\nPROCEED-ALLOWED\nReady\n"

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": "/tmp/project",
                        "ready_check_lines": 1,
                        "blocked_recovery": True,
                        "blocked_recovery_session_id": "session-id",
                        "prompts": ["prompt"],
                    }
                )
            )
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = FakeController(config, "session", "%1", "%2")
            controller.recovery_check_dir.mkdir(parents=True)

            result = controller.check_recovery_marker(
                RunItem(index=1, prompt_name="prompt-1", prompt_text="prompt", prompt_source="test", command="cdx"),
                recovery_attempts=0,
            )

            self.assertEqual(result, "proceed")
            self.assertEqual(controller.recovery_marker_line, "PROCEED-ALLOWED")

    def test_recovery_human_marker_wins_over_proceed_marker(self) -> None:
        from tempfile import TemporaryDirectory

        class FakeController(Controller):
            def __init__(self, *args: object) -> None:
                super().__init__(*args)
                self.calls = 0

            def capture_pane_tail(self, pane: str, lines: int) -> str:
                self.calls += 1
                if self.calls == 1:
                    return "Ready\n"
                return "PROCEED-ALLOWED\nHUMAN-DECISION-REQUIRED\nReady\n"

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": "/tmp/project",
                        "ready_check_lines": 1,
                        "blocked_recovery": True,
                        "blocked_recovery_session_id": "session-id",
                        "prompts": ["prompt"],
                    }
                )
            )
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = FakeController(config, "session", "%1", "%2")
            controller.recovery_check_dir.mkdir(parents=True)

            result = controller.check_recovery_marker(
                RunItem(index=1, prompt_name="prompt-1", prompt_text="prompt", prompt_source="test", command="cdx"),
                recovery_attempts=0,
            )

            self.assertEqual(result, "human")
            self.assertEqual(controller.recovery_marker_line, "HUMAN-DECISION-REQUIRED")

    def test_completion_notify_waits_for_ready_bottom_line(self) -> None:
        from tempfile import TemporaryDirectory

        class FakeController(Controller):
            def capture_pane_tail(self, pane: str, lines: int) -> str:
                return "Verification finished\n"

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": "/tmp/project",
                        "ready_check_lines": 1,
                        "completion_notify": True,
                        "completion_notify_session_id": "session-id",
                        "prompts": ["prompt"],
                    }
                )
            )
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = FakeController(config, "session", "%1", "%2")
            controller.completion_check_dir.mkdir(parents=True)

            result = controller.check_completion_ready()

            self.assertEqual(result, "waiting")

    def test_completion_notify_ready_matches_bottom_line(self) -> None:
        from tempfile import TemporaryDirectory

        class FakeController(Controller):
            def capture_pane_tail(self, pane: str, lines: int) -> str:
                return "Ready\n"

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": "/tmp/project",
                        "ready_check_lines": 1,
                        "completion_notify": True,
                        "completion_notify_session_id": "session-id",
                        "prompts": ["prompt"],
                    }
                )
            )
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = FakeController(config, "session", "%1", "%2")
            controller.completion_check_dir.mkdir(parents=True)

            result = controller.check_completion_ready()

            self.assertEqual(result, "ready")

    def test_completion_prompt_file_replaces_generic_finisher_instructions(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            project_dir = tmp_path / "project"
            project_dir.mkdir()
            (tmp_path / "finish.md").write_text(
                "Review the complete diff, then commit, push, and open the authorized PR.\n"
            )
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": str(project_dir),
                        "completion_prompt_file": "finish.md",
                        "prompts": ["one"],
                    }
                )
            )
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = Controller(config, "session", "%worker", "%planner")
            controller.completion_prompt_dir.mkdir(parents=True)

            prompt_path = controller.write_completion_prompt_file()
            prompt_text = prompt_path.read_text()

            self.assertIn("Target repo:", prompt_text)
            self.assertIn("commit, push, and open the authorized PR", prompt_text)
            self.assertNotIn("Do not commit unless explicitly requested.", prompt_text)

    def test_default_work_base_dir_lives_under_project_planning_work(self) -> None:
        self.assertEqual(
            default_work_base_dir(Path("/repo")),
            Path("/repo/.planning/work/prompt-queue"),
        )

    def test_make_runtime_dir_uses_start_timestamp_under_work_base(self) -> None:
        self.assertEqual(
            make_runtime_dir(Path("/repo"), "20260526-175900"),
            Path("/repo/.planning/work/prompt-queue/20260526-175900"),
        )

    def test_tagged_runtime_dirs_are_isolated_by_queue_id(self) -> None:
        self.assertEqual(
            make_runtime_dir(Path("/repo"), "20260526-175900", "frontend-release"),
            Path("/repo/.planning/work/prompt-queue/frontend-release/20260526-175900"),
        )

    def test_archive_queue_directory_moves_the_whole_self_contained_folder(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            source = tmp_path / "queues" / "frontend-release"
            (source / "prompts").mkdir(parents=True)
            (source / "config.json").write_text("{}")
            (source / ".env.local").write_text("PROMPT_QUEUE_WORKDIR=/repo\n")
            (source / "prompts" / "001-work.md").write_text("work")

            destination = archive_queue_directory(
                source,
                "frontend-release",
                "20260712-120000",
                tmp_path / "archive",
            )

            self.assertEqual(destination, tmp_path / "archive" / "20260712-120000-frontend-release")
            self.assertFalse(source.exists())
            self.assertEqual((destination / "prompts" / "001-work.md").read_text(), "work")

    def test_latest_runtime_dir_finds_newest_timestamped_run(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            project_dir = Path(raw_dir) / "project"
            older = project_dir / ".planning/work/prompt-queue/20260526-100000"
            newer = project_dir / ".planning/work/prompt-queue/20260526-110000"
            older.mkdir(parents=True)
            newer.mkdir(parents=True)
            (older / "session.json").write_text("{}")
            (newer / "session.json").write_text("{}")

            self.assertEqual(latest_runtime_dir(project_dir), newer)

    def test_resumable_completed_indices_reads_prior_state_when_queue_matches(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            runtime_dir = tmp_path / "runtime"
            runtime_dir.mkdir()
            config_path.write_text(
                json.dumps(
                    {
                        "prompts": [
                            {"name": "first", "text": "Prompt one"},
                            {"name": "second", "text": "Prompt two"},
                        ]
                    }
                )
            )
            prompts = load_config(config_path).prompts
            write_prompt_queue(runtime_dir / "queue.json", prompts)
            (runtime_dir / "state.json").write_text(json.dumps({"completed": [1]}))

            completed = read_resumable_completed_indices(runtime_dir, prompts)

            self.assertEqual(completed, {1})

    def test_resumable_completed_indices_ignores_changed_prompt_text(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            old_config_path = tmp_path / "old-config.json"
            new_config_path = tmp_path / "new-config.json"
            runtime_dir = tmp_path / "runtime"
            runtime_dir.mkdir()
            old_config_path.write_text(json.dumps({"prompts": [{"name": "first", "text": "Prompt one"}]}))
            new_config_path.write_text(json.dumps({"prompts": [{"name": "first", "text": "Changed prompt"}]}))
            old_prompts = load_config(old_config_path).prompts
            new_prompts = load_config(new_config_path).prompts
            write_prompt_queue(runtime_dir / "queue.json", old_prompts)
            (runtime_dir / "state.json").write_text(json.dumps({"completed": [1]}))

            completed = read_resumable_completed_indices(runtime_dir, new_prompts)

            self.assertEqual(completed, set())

    def test_controller_records_prompt_finish_progress_and_timing_log(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps({"project_dir": "/tmp/project", "prompts": ["prompt"]}))
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = Controller(config, "session", "%1")
            item = RunItem(index=1, prompt_name="prompt-1", prompt_text="prompt", prompt_source="test", command="cdx")
            controller.completed = {1}
            controller.current_prompt_started_at = 100.0
            controller.current_prompt_started_wall_at = "2026-06-29T10:00:00"

            with mock.patch.object(prompt_queue.time, "monotonic", return_value=165.25):
                controller.record_prompt_finished(item, "complete")

            progress = json.loads((config.runtime_dir / "progress.json").read_text())
            timing_lines = (config.runtime_dir / "timings.jsonl").read_text().splitlines()
            self.assertEqual(progress["completed"], [1])
            self.assertEqual(progress["last_finished"]["run_index"], 1)
            self.assertEqual(progress["last_finished"]["status"], "complete")
            self.assertEqual(progress["last_finished"]["duration_seconds"], 65.25)
            self.assertEqual(len(timing_lines), 1)
            self.assertEqual(json.loads(timing_lines[0])["duration_seconds"], 65.25)

    def test_run_one_keeps_worker_open_when_stop_after_current_is_armed(self) -> None:
        from tempfile import TemporaryDirectory

        class FakeController(Controller):
            def __init__(self, *args: object) -> None:
                super().__init__(*args)
                self.stop_calls = 0

            def recycle_worker(self) -> None:
                return None

            def cd_worker(self) -> None:
                return None

            def send_shell_command(self, command: str) -> None:
                return None

            def sleep_with_controls(self, seconds: int, phase: str, ready_item: RunItem | None = None) -> str:
                return "elapsed"

            def wait_for_worker_ready(self, phase: str, ready_item: RunItem) -> str:
                (self.config.runtime_dir / "stop-next.flag").write_text("1\n")
                return "ready"

            def capture_run(self, item: RunItem) -> Path:
                path = self.config.runtime_dir / "capture.txt"
                path.write_text("capture")
                return path

            def stop_worker(self) -> None:
                self.stop_calls += 1

            def render(self) -> None:
                return None

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps({"project_dir": "/tmp/project", "prompts": ["prompt"]}))
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = FakeController(config, "session", "%1")
            controller.prompt_dir.mkdir(parents=True)
            controller.capture_dir.mkdir(parents=True)
            item = RunItem(index=1, prompt_name="prompt-1", prompt_text="prompt", prompt_source="test", command="cdx")

            result = controller.run_one(item)

            self.assertEqual(result, "complete")
            self.assertEqual(controller.completed, {1})
            self.assertEqual(controller.stop_calls, 0)

    def test_run_one_stops_worker_after_completion_when_not_stopping(self) -> None:
        from tempfile import TemporaryDirectory

        class FakeController(Controller):
            def __init__(self, *args: object) -> None:
                super().__init__(*args)
                self.stop_calls = 0

            def recycle_worker(self) -> None:
                return None

            def cd_worker(self) -> None:
                return None

            def send_shell_command(self, command: str) -> None:
                return None

            def sleep_with_controls(self, seconds: int, phase: str, ready_item: RunItem | None = None) -> str:
                return "elapsed"

            def wait_for_worker_ready(self, phase: str, ready_item: RunItem) -> str:
                return "ready"

            def capture_run(self, item: RunItem) -> Path:
                path = self.config.runtime_dir / "capture.txt"
                path.write_text("capture")
                return path

            def stop_worker(self) -> None:
                self.stop_calls += 1

            def render(self) -> None:
                return None

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps({"project_dir": "/tmp/project", "prompts": ["prompt"]}))
            config = load_config(config_path, runtime_dir_override=tmp_path / "runtime")
            controller = FakeController(config, "session", "%1")
            controller.prompt_dir.mkdir(parents=True)
            controller.capture_dir.mkdir(parents=True)
            item = RunItem(index=1, prompt_name="prompt-1", prompt_text="prompt", prompt_source="test", command="cdx")

            result = controller.run_one(item)

            self.assertEqual(result, "complete")
            self.assertEqual(controller.stop_calls, 1)

    def test_controller_archives_tagged_queue_only_after_successful_completion(self) -> None:
        from tempfile import TemporaryDirectory

        class CompletingController(Controller):
            def run_one(self, item: RunItem, recovery_attempts: int = 0) -> str:
                self.completed.add(item.index)
                return "complete"

            def render(self) -> None:
                return

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            project_dir = tmp_path / "repo"
            project_dir.mkdir()
            source = tmp_path / "queues" / "release"
            source.mkdir(parents=True)
            config_path = source / "config.json"
            config_path.write_text(json.dumps({"project_dir": str(project_dir), "prompts": ["prompt"]}))
            runtime_dir = tmp_path / "runtime"
            config = load_config(config_path, runtime_dir_override=runtime_dir)
            controller = CompletingController(
                config,
                "session",
                "%worker",
                queue_id="release",
                queue_dir_path=source,
                archive_root=tmp_path / "archive",
                archive_stamp="20260712-120000",
            )

            controller.run()

            destination = tmp_path / "archive" / "20260712-120000-release"
            self.assertTrue((destination / "config.json").exists())
            self.assertFalse(source.exists())
            archived = json.loads((runtime_dir / "archived.json").read_text())
            self.assertEqual(archived["destination"], str(destination))

    def test_start_session_attaches_when_existing_session_choice_is_attach(self) -> None:
        from argparse import Namespace
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            project_dir = tmp_path / "repo"
            project_dir.mkdir()
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps({"project_dir": str(project_dir), "prompts": ["prompt"]}))
            args = Namespace(config=str(config_path), cld=False, session="", no_attach=False)

            with (
                mock.patch.object(prompt_queue.shutil, "which", return_value="/usr/bin/tmux"),
                mock.patch.object(prompt_queue, "tmux_session_exists", return_value=True),
                mock.patch.object(prompt_queue, "prompt_existing_session_action", return_value="attach"),
                mock.patch.object(prompt_queue, "attach_session") as attach_session,
                mock.patch.object(prompt_queue, "kill_tmux_session_if_exists") as kill_session,
            ):
                prompt_queue.start_session(args)

            attach_session.assert_called_once_with("prompt-queue-repo")
            kill_session.assert_not_called()

    def test_start_session_no_confirm_skips_interactive_new_run_prompt(self) -> None:
        from argparse import Namespace
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            project_dir = tmp_path / "repo"
            project_dir.mkdir()
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps({"project_dir": str(project_dir), "prompts": ["prompt"]}))
            args = Namespace(config=str(config_path), cld=False, session="", no_attach=True, no_confirm=True)
            pane_ids = iter(["%controller", "%prompt-list", "%worker"])

            def fake_tmux(*tmux_args: str, **kwargs: object) -> CompletedProcess[str]:
                if tmux_args[0] in {"new-session", "split-window"}:
                    return CompletedProcess(["tmux", *tmux_args], 0, next(pane_ids), "")
                return CompletedProcess(["tmux", *tmux_args], 0, "", "")

            with (
                mock.patch.object(prompt_queue.shutil, "which", return_value="/usr/bin/tmux"),
                mock.patch.object(prompt_queue, "tmux_session_exists", return_value=False),
                mock.patch.object(prompt_queue.sys.stdin, "isatty", return_value=True),
                mock.patch.object(prompt_queue, "prompt_start_new_run") as prompt_start_new_run,
                mock.patch.object(prompt_queue, "tmux", side_effect=fake_tmux),
            ):
                prompt_queue.start_session(args)

            prompt_start_new_run.assert_not_called()

    def test_start_session_replaces_stale_controller_without_prompting(self) -> None:
        from argparse import Namespace
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            project_dir = tmp_path / "repo"
            project_dir.mkdir()
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps({"project_dir": str(project_dir), "prompts": ["prompt"]}))
            args = Namespace(config=str(config_path), cld=False, session="", no_attach=True)
            pane_ids = iter(["%controller", "%prompt-list", "%worker"])

            def fake_tmux(*tmux_args: str, **kwargs: object) -> CompletedProcess[str]:
                if tmux_args[0] in {"new-session", "split-window"}:
                    return CompletedProcess(["tmux", *tmux_args], 0, next(pane_ids), "")
                return CompletedProcess(["tmux", *tmux_args], 0, "", "")

            with (
                mock.patch.object(prompt_queue.shutil, "which", return_value="/usr/bin/tmux"),
                mock.patch.object(prompt_queue, "tmux_session_exists", return_value=True),
                mock.patch.object(prompt_queue, "controller_process_is_running", return_value=False),
                mock.patch.object(prompt_queue, "prompt_existing_session_action") as choose_action,
                mock.patch.object(prompt_queue, "prepare_tmux_session_replacement", return_value="") as replace_session,
                mock.patch.object(prompt_queue, "tmux", side_effect=fake_tmux),
                mock.patch("builtins.print"),
            ):
                prompt_queue.start_session(args)

            choose_action.assert_not_called()
            replace_session.assert_called_once_with("prompt-queue-repo")

    def test_start_session_replaces_stale_session_from_inside_its_worker_pane(self) -> None:
        from argparse import Namespace
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            project_dir = tmp_path / "repo"
            project_dir.mkdir()
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps({"project_dir": str(project_dir), "prompts": ["prompt"]}))
            args = Namespace(config=str(config_path), cld=False, session="", no_attach=False)
            calls: list[tuple[str, ...]] = []
            pane_ids = iter(["%controller", "%prompt-list", "%worker"])

            def fake_tmux(*tmux_args: str, **kwargs: object) -> CompletedProcess[str]:
                calls.append(tmux_args)
                if tmux_args[0] in {"new-session", "split-window"}:
                    return CompletedProcess(["tmux", *tmux_args], 0, next(pane_ids), "")
                return CompletedProcess(["tmux", *tmux_args], 0, "", "")

            with (
                mock.patch.object(prompt_queue.shutil, "which", return_value="/usr/bin/tmux"),
                mock.patch.object(prompt_queue, "tmux_session_exists", return_value=True),
                mock.patch.object(prompt_queue, "controller_process_is_running", return_value=False),
                mock.patch.object(
                    prompt_queue,
                    "prepare_tmux_session_replacement",
                    return_value="prompt-queue-repo-stale-4321",
                ),
                mock.patch.object(prompt_queue, "tmux", side_effect=fake_tmux),
                mock.patch.object(prompt_queue, "attach_session") as attach_session,
                mock.patch("builtins.print"),
            ):
                prompt_queue.start_session(args)

            self.assertIn(("switch-client", "-t", "prompt-queue-repo"), calls)
            controller_send = next(
                call for call in calls if call[:3] == ("send-keys", "-t", "%controller")
            )
            self.assertTrue(
                controller_send[3].startswith(
                    "tmux kill-session -t 'prompt-queue-repo-stale-4321'; python3 "
                )
            )
            attach_session.assert_not_called()

    def test_start_session_uses_tagged_config_runtime_and_tmux_session(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            project_dir = tmp_path / "repo"
            project_dir.mkdir()
            queues_root = tmp_path / "queues"
            queue_folder = queues_root / "blue-release"
            queue_folder.mkdir(parents=True)
            (queue_folder / "config.json").write_text(
                json.dumps({"project_dir": str(project_dir), "prompts": ["prompt"]})
            )
            args = prompt_queue.build_parser().parse_args(["run", "blue-release", "--no-attach"])
            calls: list[tuple[str, ...]] = []
            pane_ids = iter(["%controller", "%prompt-list", "%worker"])

            def fake_tmux(*tmux_args: str, **kwargs: object) -> CompletedProcess[str]:
                calls.append(tmux_args)
                if tmux_args[0] in {"new-session", "split-window"}:
                    return CompletedProcess(["tmux", *tmux_args], 0, next(pane_ids), "")
                return CompletedProcess(["tmux", *tmux_args], 0, "", "")

            with (
                mock.patch.object(prompt_queue, "QUEUES_DIR", queues_root),
                mock.patch.object(prompt_queue, "ARCHIVE_DIR", tmp_path / "archive"),
                mock.patch.object(prompt_queue.shutil, "which", return_value="/usr/bin/tmux"),
                mock.patch.object(prompt_queue, "tmux_session_exists", return_value=False),
                mock.patch.object(prompt_queue.sys.stdin, "isatty", return_value=False),
                mock.patch.object(prompt_queue, "tmux", side_effect=fake_tmux),
                mock.patch("builtins.print"),
            ):
                prompt_queue.start_session(args)

            new_session = next(call for call in calls if call[0] == "new-session")
            self.assertEqual(new_session[new_session.index("-s") + 1], "prompt-queue-blue-release-repo")
            new_session_index = calls.index(new_session)
            worker_split = next(
                call for call in calls if call[0] == "split-window" and "%prompt-list" in call
            )
            worker_split_index = calls.index(worker_split)
            environment_calls = [
                index for index, call in enumerate(calls) if call[0] == "set-environment"
            ]
            self.assertTrue(environment_calls)
            self.assertTrue(all(new_session_index < index < worker_split_index for index in environment_calls))
            controller_send = next(
                call for call in calls if call[:3] == ("send-keys", "-t", "%controller")
            )
            self.assertIn("--queue-id 'blue-release'", controller_send[3])
            self.assertIn(str(project_dir / ".planning/work/prompt-queue/blue-release"), controller_send[3])
            self.assertIn(f"--queue-dir '{queue_folder}'", controller_send[3])

    def test_start_session_layout_places_controller_over_planner_and_prompt_list_over_worker(self) -> None:
        from argparse import Namespace
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            tmp_path = Path(raw_dir)
            project_dir = tmp_path / "repo"
            project_dir.mkdir()
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project_dir": str(project_dir),
                        "blocked_recovery": True,
                        "blocked_recovery_session_id": "planner-session",
                        "prompts": ["prompt"],
                    }
                )
            )
            args = Namespace(config=str(config_path), cld=False, session="", no_attach=True)
            calls: list[tuple[str, ...]] = []
            pane_ids = iter(["%controller", "%prompt-list", "%planner", "%worker"])

            def fake_tmux(*tmux_args: str, **kwargs: object) -> CompletedProcess[str]:
                calls.append(tmux_args)
                if tmux_args[0] in {"new-session", "split-window"}:
                    return CompletedProcess(["tmux", *tmux_args], 0, next(pane_ids), "")
                return CompletedProcess(["tmux", *tmux_args], 0, "", "")

            with (
                mock.patch.object(prompt_queue.shutil, "which", return_value="/usr/bin/tmux"),
                mock.patch.object(prompt_queue, "tmux_session_exists", return_value=False),
                mock.patch.object(prompt_queue.sys.stdin, "isatty", return_value=False),
                mock.patch.object(prompt_queue, "tmux", side_effect=fake_tmux),
                mock.patch("builtins.print"),
            ):
                prompt_queue.start_session(args)

            split_calls = [call for call in calls if call[0] == "split-window"]
            self.assertEqual(split_calls[0][1:4], ("-t", "%controller", "-h"))
            self.assertIn("%controller", split_calls[1])
            self.assertIn("-v", split_calls[1])
            self.assertIn("%prompt-list", split_calls[2])
            self.assertIn("-v", split_calls[2])


if __name__ == "__main__":
    unittest.main()
