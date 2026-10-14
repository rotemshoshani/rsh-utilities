import json
import unittest
from pathlib import Path

from advisor import (
    Agent,
    CommandConfig,
    Controller,
    ModelOption,
    RunItem,
    agent_from_model,
    build_model_command,
    build_incomplete_continuation_prompt,
    build_parser,
    build_prompt_at_file_command,
    build_review_screen,
    build_prompt_argument_command,
    build_run_queue,
    build_worker_cd_command,
    config_to_resolved_json,
    completion_file_path,
    consume_finish_current_sleep,
    default_work_base_dir,
    latest_runtime_dir,
    last_non_empty_line,
    load_config,
    make_runtime_dir,
    missing_required_artifacts,
    ready_marker_match,
    reviewer_agent_for_cli,
    required_artifact_paths,
    render_prompt,
    render_status,
    resolve_config,
    select_models,
    should_check_ready_for_item,
    should_finish_current_sleep,
    should_stop_after_current,
    topic_raw_for,
)


class AdvisorTests(unittest.TestCase):
    def test_arch_preset_uses_arch_defaults_and_agent_delivery(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.json", topic_override="arch")

        self.assertEqual(config.topic, "arch")
        self.assertEqual(config.work_dir_name, "arch-advisor")
        self.assertEqual(config.num_runs, 3)
        self.assertEqual(config.run_seconds, 1500)
        self.assertEqual(config.ready_check_seconds, 5)
        self.assertEqual(config.ready_check_lines, 1)
        self.assertEqual(config.ready_markers, ("Ready",))
        self.assertEqual(config.ready_command_names, ("codex", "pi"))
        self.assertEqual([agent.name for agent in config.agents], ["codex-Codex-default", "claude-Claude-default"])
        self.assertEqual([agent.command for agent in config.agents], ["cdx", "cld"])
        self.assertEqual(config.agents[0].prompt_delivery, "argument_file")
        self.assertEqual(config.agents[1].prompt_delivery, "paste")

    def test_hygiene_preset_checks_reachability_and_cleanup(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.json", topic_override="hygiene")
        prompt = render_prompt(
            config.prompt,
            config,
            RunItem(index=1, cycle=1, agent_name="codex", command="cdx"),
        )

        self.assertEqual(config.topic, "hygiene")
        self.assertEqual(config.work_dir_name, "hygiene-advisor")
        self.assertEqual(config.session_name, "hygiene-advisor")
        self.assertEqual([agent.name for agent in config.agents], ["codex-Codex-default", "claude-Claude-default"])
        self.assertIn("map the main reachable product surface", prompt)
        self.assertIn("not reachable from navigation", prompt)
        self.assertIn("duplicate or near-duplicate components", prompt)
        self.assertIn("/.planning/work/hygiene-advisor", str(config.runtime_dir))
        self.assertIn("hygiene-findings.md", prompt)
        self.assertIn("cleanup-plan.md", prompt)

    def test_sec_preset_uses_sec_defaults(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.json", topic_override="sec")

        self.assertEqual(config.topic, "sec")
        self.assertEqual(config.work_dir_name, "sec-advisor")
        self.assertEqual(config.num_runs, 6)
        self.assertEqual(config.run_seconds, 5400)
        self.assertEqual([agent.name for agent in config.agents], ["claude-claude-fable-5"])
        self.assertEqual(config.agents[0].command, "cld --model claude-fable-5")
        self.assertEqual(config.agents[0].model_label, "Claude Fable 5")

    def test_attack_preset_reads_previous_runs_before_searching(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.json", topic_override="attack")
        prompt = render_prompt(
            config.prompt,
            config,
            RunItem(index=2, cycle=2, agent_name="pi-z-ai-glm-5.3-flash", command="pi"),
        )

        self.assertEqual(config.topic, "attack")
        self.assertEqual(config.work_dir_name, "attack-advisor")
        self.assertEqual(config.session_name, "attack-advisor")
        self.assertEqual(config.num_runs, 6)
        self.assertEqual(config.run_seconds, 5400)
        self.assertEqual([agent.model for agent in config.agents], ["z-ai/glm-5.3-flash"])
        self.assertIn("export OPENROUTER_API_KEY; pi --approve --provider openrouter --extension", config.agents[0].command)
        self.assertIn("pi_glm_5_3_flash.js --model z-ai/glm-5.3-flash --thinking max", config.agents[0].command)
        self.assertNotIn("python3", config.agents[0].command)
        self.assertNotIn("__exec-with-env", config.agents[0].command)
        self.assertEqual(config.agents[0].command_name, "pi")
        self.assertEqual(config.agents[0].prompt_delivery, "at_file")
        self.assertEqual(config.agents[0].completion_marker, "")
        self.assertEqual(config.agents[0].completion_file, ".pi-ready-{run_index}")
        self.assertEqual(
            config.required_artifacts,
            (
                "run-{run_index}-{agent_name}-attack-findings.md",
                "run-{run_index}-{agent_name}-remediation-proposal.md",
            ),
        )
        self.assertEqual(config.required_artifact_marker, "")
        self.assertEqual(config.required_artifact_min_bytes, 500)
        self.assertEqual(config.incomplete_artifact_markers, ("_(to be filled", "_(pending)_"))
        self.assertEqual(config.max_incomplete_continuations, 4)
        self.assertEqual(config.reviewer_artifact_min_bytes, 1000)
        self.assertIn("Do not stop after the previous-run intake", prompt)
        self.assertNotIn("20 non-output tool calls", prompt)
        self.assertTrue(prompt.rstrip().endswith("before becoming idle."))
        self.assertLess(prompt.index("Your first action"), prompt.index("Then map the reachable attack surface"))
        self.assertIn("Do not begin repository searches until this intake is complete", prompt)
        self.assertIn("run-2-pi-z-ai-glm-5.3-flash-attack-findings.md", prompt)
        self.assertIn("run-2-pi-z-ai-glm-5.3-flash-remediation-proposal.md", prompt)

    def test_attack_required_artifacts_must_exist_and_be_non_empty(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir) / "attack-run"
            runtime_dir.mkdir()
            config = load_config(
                Path(__file__).parents[1] / "config.json",
                topic_override="attack",
                runtime_dir_override=runtime_dir,
            )
            item = RunItem(index=3, cycle=3, agent_name="pi-z-ai-glm-5.3-flash", command="pi")
            paths = required_artifact_paths(config, item)

            self.assertEqual(
                paths,
                (
                    runtime_dir / "run-3-pi-z-ai-glm-5.3-flash-attack-findings.md",
                    runtime_dir / "run-3-pi-z-ai-glm-5.3-flash-remediation-proposal.md",
                ),
            )
            self.assertEqual(missing_required_artifacts(config, item), paths)

            paths[0].write_text("# Findings\n_(to be filled with evidence)_\n" + "context\n" * 100)
            paths[1].write_text("# Proposal\n" + "substantive remediation evidence\n" * 20)
            self.assertEqual(missing_required_artifacts(config, item), (paths[0],))

            paths[0].write_text("# Findings\n" + "substantive repository evidence\n" * 20)
            self.assertEqual(missing_required_artifacts(config, item), ())

            ready_item = RunItem(
                index=3,
                cycle=3,
                agent_name="pi-z-ai-glm-5.3-flash",
                command="pi",
                completion_file=".pi-ready-{run_index}",
            )
            self.assertEqual(completion_file_path(config, ready_item), runtime_dir / ".pi-ready-3")

            reviewer = RunItem(
                index=4,
                cycle=4,
                agent_name="reviewer-pi",
                command="pi",
                prompt_kind="reviewer",
            )
            final_review = runtime_dir / "final-review.html"
            self.assertEqual(required_artifact_paths(config, reviewer), (final_review,))
            final_review.write_text("<html><body>short</body></html>")
            self.assertEqual(missing_required_artifacts(config, reviewer), (final_review,))
            final_review.write_text("<!doctype html><html><body>" + "review evidence " * 100 + "</body></html>")
            self.assertEqual(missing_required_artifacts(config, reviewer), ())

    def test_incomplete_continuation_prompt_requires_finished_files(self) -> None:
        paths = (Path("/run/findings.md"), Path("/run/proposal.md"))

        prompt = build_incomplete_continuation_prompt(paths)

        self.assertIn("same Pi session", prompt)
        self.assertIn("/run/findings.md", prompt)
        self.assertIn("/run/proposal.md", prompt)
        self.assertIn("replace every placeholder", prompt)
        self.assertIn("Finish the files themselves", prompt)

    def test_ready_placeholder_output_is_continued_before_completion(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        with TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir) / "attack-run"
            runtime_dir.mkdir()
            config = load_config(
                Path(__file__).parents[1] / "config.json",
                topic_override="attack",
                runtime_dir_override=runtime_dir,
            )
            item = RunItem(
                index=1,
                cycle=1,
                agent_name="pi-z-ai-glm-5.3-flash",
                command="pi",
                command_name="pi",
                completion_file=".pi-ready-{run_index}",
            )
            paths = required_artifact_paths(config, item)
            controller = Controller(config, "attack-advisor", "%1")
            ticks = iter(range(1000))

            def record_continuation(_item: RunItem, missing: tuple[Path, ...]) -> None:
                controller.incomplete_continuations += 1
                self.assertEqual(missing, paths)

            with (
                patch("advisor.time.time", side_effect=lambda: next(ticks)),
                patch("advisor.time.sleep"),
                patch("advisor.missing_required_artifacts", side_effect=[paths, ()]),
                patch.object(controller, "check_ready_marker", return_value=True),
                patch.object(controller, "continue_incomplete_run", side_effect=record_continuation) as continued,
                patch.object(controller, "handle_keyboard"),
                patch.object(controller, "render"),
            ):
                result = controller.wait_for_ready_or_timeout(100, "agent working", item)

            self.assertEqual(result, "ready")
            continued.assert_called_once_with(item, paths)
            self.assertEqual(controller.incomplete_continuations, 1)
            self.assertEqual(controller.missing_required_artifacts, [])

    def test_incomplete_run_followup_is_sent_into_same_pi_session(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        with TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir) / "attack-run"
            runtime_dir.mkdir()
            config = load_config(
                Path(__file__).parents[1] / "config.json",
                topic_override="attack",
                runtime_dir_override=runtime_dir,
            )
            item = RunItem(
                index=1,
                cycle=1,
                agent_name="pi-z-ai-glm-5.3-flash",
                command="pi",
                command_name="pi",
                completion_file=".pi-ready-{run_index}",
            )
            missing = required_artifact_paths(config, item)
            ready_file = runtime_dir / ".pi-ready-1"
            ready_file.write_text("Ready\n")
            controller = Controller(config, "attack-advisor", "%1")

            with (
                patch.object(controller, "paste_prompt") as paste_prompt,
                patch.object(controller, "send_key") as send_key,
                patch.object(controller, "render"),
            ):
                controller.continue_incomplete_run(item, missing)

            self.assertFalse(ready_file.exists())
            self.assertEqual(controller.incomplete_continuations, 1)
            continuation, filename = paste_prompt.call_args.args
            self.assertIn("same Pi session", continuation)
            self.assertEqual(filename, "continuation-prompt-1.txt")
            send_key.assert_called_once_with("Enter")
            self.assertTrue((runtime_dir / "last-incomplete-continuation.json").is_file())

    def test_pi_followup_uses_bracketed_multiline_paste(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        with TemporaryDirectory() as raw_dir:
            config = load_config(
                Path(__file__).parents[1] / "config.json",
                topic_override="attack",
                runtime_dir_override=Path(raw_dir),
            )
            controller = Controller(config, "attack-advisor", "%1")

            with patch("advisor.tmux") as tmux_call:
                controller.paste_prompt("first line\nsecond line", "continuation.txt")

            paste_call = tmux_call.call_args_list[-1]
            self.assertEqual(paste_call.args[:4], ("paste-buffer", "-d", "-p", "-r"))

    def test_pi_error_signal_stops_without_artifact_continuations(self) -> None:
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        with TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir)
            config = load_config(
                Path(__file__).parents[1] / "config.json",
                topic_override="attack",
                runtime_dir_override=runtime_dir,
            )
            item = RunItem(
                index=1,
                cycle=1,
                agent_name="pi-z-ai-glm-5.3-flash",
                command="pi",
                command_name="pi",
                completion_file=".pi-ready-{run_index}",
            )
            (runtime_dir / ".pi-ready-1").write_text("Error\n")
            controller = Controller(config, "attack-advisor", "%1")
            ticks = iter(range(1000))

            with (
                patch("advisor.time.time", side_effect=lambda: next(ticks)),
                patch("advisor.time.sleep"),
                patch.object(controller, "handle_keyboard"),
                patch.object(controller, "render"),
                patch.object(controller, "continue_incomplete_run") as continued,
            ):
                result = controller.wait_for_ready_or_timeout(100, "agent working", item)

            self.assertEqual(result, "error")
            continued.assert_not_called()

    def test_no_tui_canary_flags_are_parsed(self) -> None:
        args = build_parser().parse_args(
            [
                "run",
                "--no-tui",
                "--topic",
                "attack",
                "--cycles",
                "1",
                "--seconds",
                "600",
                "--reviewer",
                "pi",
                "--reviewer-model",
                "z-ai/glm-5.3-flash",
                "/repo",
            ]
        )

        self.assertTrue(args.no_tui)
        self.assertEqual(args.topic, "attack")
        self.assertEqual(args.cycles, 1)
        self.assertEqual(args.seconds, 600)
        self.assertEqual(args.reviewer, "pi")
        self.assertEqual(args.reviewer_model, "z-ai/glm-5.3-flash")
        self.assertEqual(args.project_dir, "/repo")

    def test_cli_selects_glm_reviewer(self) -> None:
        raw = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        reviewer = reviewer_agent_for_cli(
            raw,
            topic_raw_for(raw, "attack"),
            "pi",
            "z-ai/glm-5.3-flash",
        )

        self.assertEqual(reviewer.command_name, "pi")
        self.assertEqual(reviewer.model, "z-ai/glm-5.3-flash")
        self.assertIn("--thinking max", reviewer.command)

    def test_sec_model_picker_includes_openai_models(self) -> None:
        from unittest.mock import patch

        raw = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        topic_raw = topic_raw_for(raw, "sec")
        labels_seen: list[str] = []

        def fake_select(prompt: str, options: list[tuple[str, str]], multi: bool = False) -> list[str]:
            nonlocal labels_seen
            labels_seen = [label for label, _ in options]
            return ["codex:2"]

        with patch("advisor.fzf_select", side_effect=fake_select):
            agents = select_models(raw, topic_raw)

        self.assertTrue(any("OpenAI GPT-5.6 Sol" in label for label in labels_seen))
        self.assertTrue(any("OpenAI GPT-5.6 Terra" in label for label in labels_seen))
        self.assertTrue(any("OpenAI GPT-5.6 Luna" in label for label in labels_seen))
        self.assertEqual([agent.command for agent in agents], ["cdx --model gpt-5.6"])

    def test_model_picker_includes_claude_opus_5(self) -> None:
        from unittest.mock import patch

        raw = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        topic_raw = topic_raw_for(raw, "arch")
        labels_seen: list[str] = []

        def fake_select(prompt: str, options: list[tuple[str, str]], multi: bool = False) -> list[str]:
            nonlocal labels_seen
            labels_seen = [label for label, _ in options]
            opus_5_key = next(key for label, key in options if "Claude Opus 5" in label)
            return [opus_5_key]

        with patch("advisor.fzf_select", side_effect=fake_select):
            agents = select_models(raw, topic_raw)

        self.assertTrue(any("Claude Opus 5 (claude-opus-5)" in label for label in labels_seen))
        self.assertEqual([agent.command for agent in agents], ["cld --model claude-opus-5"])

    def test_model_picker_includes_openrouter_glm_5_3_flash(self) -> None:
        from unittest.mock import patch

        raw = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        topic_raw = topic_raw_for(raw, "arch")
        labels_seen: list[str] = []

        def fake_select(prompt: str, options: list[tuple[str, str]], multi: bool = False) -> list[str]:
            nonlocal labels_seen
            labels_seen = [label for label, _ in options]
            glm_key = next(key for label, key in options if "GLM 5.3 Flash" in label)
            return [glm_key]

        with patch("advisor.fzf_select", side_effect=fake_select):
            agents = select_models(raw, topic_raw)

        self.assertTrue(any("OpenRouter Z.ai GLM 5.3 Flash (z-ai/glm-5.3-flash)" in label for label in labels_seen))
        self.assertIn("source -- '/home/rshoshani/projects/rsh-utilities/.env.local'", agents[0].command)
        self.assertIn("export OPENROUTER_API_KEY; pi --approve --provider openrouter --extension", agents[0].command)
        self.assertIn("pi_glm_5_3_flash.js --model z-ai/glm-5.3-flash --thinking max", agents[0].command)
        self.assertNotIn("python3", agents[0].command)
        self.assertNotIn("__exec-with-env", agents[0].command)
        self.assertNotIn("cdx", agents[0].command)
        self.assertEqual(agents[0].command_name, "pi")
        self.assertEqual(agents[0].prompt_delivery, "at_file")
        self.assertEqual(agents[0].completion_marker, "")
        self.assertEqual(agents[0].completion_file, ".pi-ready-{run_index}")
        self.assertEqual(agents[0].model, "z-ai/glm-5.3-flash")

    def test_custom_prompt_puts_task_before_operational_instructions(self) -> None:
        raw = json.loads((Path(__file__).parents[1] / "config.json").read_text())

        config = resolve_config(raw, Path("/repo"), topic_override="custom", custom_task_description='Review billing flows with {"id": 1}.')
        prompt = render_prompt(config.prompt, config, RunItem(index=1, cycle=1, agent_name="claude", command="claude"))

        self.assertIn('Task:\nReview billing flows with {"id": 1}.', prompt)
        self.assertIn("read any previous advisor files under /repo/.planning/work/advisor", prompt)
        self.assertEqual(config.work_dir_name, "advisor")

    def test_model_command_is_built_from_model_free_command_and_model_attrs(self) -> None:
        codex = CommandConfig(name="codex", command="cdx", prompt_delivery="argument_file")
        claude = CommandConfig(name="claude", command="cld", prompt_delivery="paste")

        self.assertEqual(build_model_command(codex, ModelOption(label="OpenAI GPT-5.5", model="gpt-5.5")), "cdx --model gpt-5.5")
        self.assertEqual(build_model_command(claude, ModelOption(label="Claude default", model="")), "cld")

        agent = agent_from_model(codex, ModelOption(label="OpenAI GPT-5.4 mini", model="gpt-5.4-mini"))

        self.assertEqual(agent.name, "codex-gpt-5.4-mini")
        self.assertEqual(agent.command, "cdx --model gpt-5.4-mini")
        self.assertEqual(agent.prompt_delivery, "argument_file")

    def test_ready_marker_helpers_detect_codex_ready(self) -> None:
        codex_item = RunItem(
            index=1,
            cycle=1,
            agent_name="codex-gpt-5.5",
            command="cdx --model gpt-5.5",
            command_name="codex",
        )
        claude_item = RunItem(
            index=2,
            cycle=1,
            agent_name="claude-sonnet",
            command="cld --model claude-sonnet-4-6",
            command_name="claude",
        )

        self.assertEqual(last_non_empty_line("working\n\nReady\n"), "Ready")
        self.assertEqual(ready_marker_match("status: Ready", ("Ready",)), "Ready")
        self.assertTrue(should_check_ready_for_item(codex_item, ("codex",)))
        self.assertFalse(should_check_ready_for_item(claude_item, ("codex",)))

    def test_build_run_queue_repeats_each_agent_for_num_runs(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "resolved": True,
                        "project_dir": "/repo",
                        "runtime_dir": "/repo/.planning/work/advisor/run",
                        "num_runs": 2,
                        "ready_check_seconds": 15,
                        "ready_check_lines": 2,
                        "ready_markers": ["Ready", "Done"],
                        "ready_command_names": ["codex", "other"],
                        "agents": [
                            {"name": "claude", "command": "claude --model default"},
                            {"name": "codex", "command": "codex"},
                        ],
                    }
                )
            )

            config = load_config(config_path)
            queue = build_run_queue(config)

            self.assertEqual(
                [(item.index, item.agent_name, item.command) for item in queue],
                [
                    (1, "claude", "claude --model default"),
                    (2, "codex", "codex"),
                    (3, "claude", "claude --model default"),
                    (4, "codex", "codex"),
                ],
            )
            self.assertEqual(config.ready_check_seconds, 15)
            self.assertEqual(config.ready_check_lines, 2)
            self.assertEqual(config.ready_markers, ("Ready", "Done"))
            self.assertEqual(config.ready_command_names, ("codex", "other"))
            self.assertEqual(config.required_artifacts, ())
            self.assertEqual(config.required_artifact_marker, "")

    def test_build_run_queue_appends_reviewer_after_cycles(self) -> None:
        raw = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        config = resolve_config(
            raw,
            Path("/repo"),
            topic_override="custom",
            selected_agents=(
                Agent(name="codex", command="cdx", prompt_delivery="argument_file"),
                Agent(name="claude", command="cld", prompt_delivery="paste"),
            ),
            reviewer_agent=Agent(
                name="reviewer-claude-sonnet",
                command="cld --model claude-sonnet-4-6",
                prompt_delivery="paste",
            ),
            num_runs_override=2,
            runtime_dir_override=Path("/repo/.planning/work/advisor/run"),
            custom_task_description="Check auth.",
        )

        queue = build_run_queue(config)

        self.assertEqual(
            [(item.index, item.command, item.prompt_kind) for item in queue],
            [
                (1, "cdx", "advisor"),
                (2, "cld", "advisor"),
                (3, "cdx", "advisor"),
                (4, "cld", "advisor"),
                (5, "cld --model claude-sonnet-4-6", "reviewer"),
            ],
        )

    def test_reviewer_prompt_points_to_final_html(self) -> None:
        raw = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        config = resolve_config(
            raw,
            Path("/repo"),
            topic_override="custom",
            selected_agents=(Agent(name="codex", command="cdx", prompt_delivery="argument_file"),),
            reviewer_agent=Agent(name="reviewer-codex", command="cdx", prompt_delivery="argument_file"),
            runtime_dir_override=Path("/repo/.planning/work/advisor/run"),
            custom_task_description="Check auth.",
        )

        prompt = render_prompt(
            config.reviewer_prompt,
            config,
            RunItem(index=2, cycle=2, agent_name="reviewer-codex", command="cdx", prompt_kind="reviewer"),
        )

        self.assertIn("Read all findings, reports, fix plans", prompt)
        self.assertIn("/repo/.planning/work/advisor/run/final-review.html", prompt)
        self.assertIn("simple to understand and good looking", prompt)
        self.assertIn("If there are no errors or actionable findings", prompt)
        self.assertIn("complete absolute file path from the filesystem root", prompt)

    def test_render_status_marks_done_in_progress_pending_and_controls(self) -> None:
        queue = [
            RunItem(index=1, cycle=1, agent_name="claude", command="claude"),
            RunItem(index=2, cycle=1, agent_name="codex", command="codex"),
            RunItem(index=3, cycle=2, agent_name="claude", command="claude"),
        ]

        status = render_status(queue, current_index=2, completed={1}, stop_after_current=True)

        self.assertIn("claude #1 ... V", status)
        self.assertIn("codex #2 ... in progress", status)
        self.assertIn("claude #3 ... queued", status)
        self.assertIn("[S] stop after current: armed", status)
        self.assertIn("[F] finish current sleep", status)

    def test_build_prompt_argument_command_reads_prompt_file_as_single_argument(self) -> None:
        command = build_prompt_argument_command("cdx", Path("/tmp/project prompt.txt"))

        self.assertEqual(command, 'cdx "$(cat \'/tmp/project prompt.txt\')"')

    def test_build_prompt_argument_command_prints_completion_marker(self) -> None:
        command = build_prompt_argument_command("pi --print", Path("/tmp/prompt.txt"), "Ready")

        self.assertEqual(command, 'pi --print "$(cat \'/tmp/prompt.txt\')"; printf \'\\n%s\\n\' \'Ready\'')

    def test_build_prompt_at_file_command_uses_pi_native_file_argument(self) -> None:
        command = build_prompt_at_file_command("pi -p", Path("/tmp/project prompt.txt"), "Ready")

        self.assertEqual(command, "pi -p '@/tmp/project prompt.txt'; printf '\\n%s\\n' 'Ready'")

    def test_stop_and_finish_flags(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            runtime_dir = Path(raw_dir) / "runtime"
            runtime_dir.mkdir()

            self.assertFalse(should_stop_after_current(runtime_dir))
            (runtime_dir / "stop-next.flag").write_text("1")
            self.assertTrue(should_stop_after_current(runtime_dir))

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

    def test_runtime_paths_use_topic_work_dir(self) -> None:
        self.assertEqual(default_work_base_dir(Path("/repo"), "arch-advisor"), Path("/repo/.planning/work/arch-advisor"))
        self.assertEqual(
            make_runtime_dir(Path("/repo"), "sec-advisor", "20260526-175900"),
            Path("/repo/.planning/work/sec-advisor/20260526-175900"),
        )

    def test_render_prompt_supports_all_advisor_dir_aliases(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            config_path = Path(raw_dir) / "config.json"
            runtime_dir = Path(raw_dir) / "repo/.planning/work/advisor/run"
            config_path.write_text(
                json.dumps(
                    {
                        "resolved": True,
                        "project_dir": str(Path(raw_dir) / "repo"),
                        "runtime_dir": str(runtime_dir),
                        "agents": [{"name": "claude", "command": "claude"}],
                        "prompt": "audit={audit_dir} review={review_dir} advisor={advisor_dir} runtime={runtime_dir}",
                    }
                )
            )

            config = load_config(config_path)
            prompt = render_prompt(config.prompt, config, RunItem(index=1, cycle=1, agent_name="claude", command="claude"))

            self.assertIn(f"audit={runtime_dir}", prompt)
            self.assertIn(f"review={runtime_dir}", prompt)
            self.assertIn(f"advisor={runtime_dir}", prompt)
            self.assertIn(f"runtime={runtime_dir}", prompt)

    def test_resolved_config_round_trips(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            raw = json.loads((Path(__file__).parents[1] / "config.json").read_text())
            config = resolve_config(
                raw,
                Path(raw_dir) / "repo",
                topic_override="arch",
                selected_agents=(
                    Agent(
                        name="codex-gpt-5.5",
                        command="cdx --model gpt-5.5",
                        command_name="codex",
                        model_label="OpenAI GPT-5.5",
                        model="gpt-5.5",
                        prompt_delivery="argument_file",
                    ),
                ),
                runtime_dir_override=Path(raw_dir) / "run",
            )
            path = Path(raw_dir) / "run-config.json"
            path.write_text(json.dumps(config_to_resolved_json(config)))

            loaded = load_config(path)

            self.assertEqual(loaded.topic, "arch")
            self.assertEqual(loaded.runtime_dir, Path(raw_dir) / "run")
            self.assertEqual([agent.name for agent in loaded.agents], ["codex-gpt-5.5"])
            self.assertEqual(loaded.agents[0].model_label, "OpenAI GPT-5.5")
            self.assertEqual(loaded.required_artifacts, config.required_artifacts)
            self.assertEqual(loaded.required_artifact_marker, config.required_artifact_marker)
            self.assertEqual(loaded.required_artifact_min_bytes, config.required_artifact_min_bytes)
            self.assertEqual(loaded.incomplete_artifact_markers, config.incomplete_artifact_markers)
            self.assertEqual(loaded.max_incomplete_continuations, config.max_incomplete_continuations)
            self.assertEqual(loaded.reviewer_artifact_min_bytes, config.reviewer_artifact_min_bytes)

    def test_review_screen_shows_prompt_models_and_run_counts(self) -> None:
        raw = json.loads((Path(__file__).parents[1] / "config.json").read_text())
        config = resolve_config(
            raw,
            Path("/repo"),
            topic_override="custom",
            selected_agents=(
                Agent(
                    name="codex-gpt-5.5",
                    command="cdx --model gpt-5.5",
                    command_name="codex",
                    model_label="OpenAI GPT-5.5",
                    model="gpt-5.5",
                    prompt_delivery="argument_file",
                ),
                Agent(
                    name="claude-claude-sonnet-4-6",
                    command="cld --model claude-sonnet-4-6",
                    command_name="claude",
                    model_label="Claude Sonnet 4.6",
                    model="claude-sonnet-4-6",
                    prompt_delivery="paste",
                ),
            ),
            reviewer_agent=Agent(
                name="reviewer-claude-opus-4-8",
                command="cld --model claude-opus-4-8",
                command_name="claude",
                model_label="Claude Opus 4.8",
                model="claude-opus-4-8",
                prompt_delivery="paste",
            ),
            num_runs_override=2,
            runtime_dir_override=Path("/repo/.planning/work/advisor/run"),
            custom_task_description="Check auth and data loading.",
        )

        review = build_review_screen(config)

        self.assertTrue(review.startswith("Topic: custom - Custom advisor task"))
        self.assertIn("Topic: custom - Custom advisor task", review)
        self.assertIn("Cycles: 2", review)
        self.assertIn("Agents per cycle: 2", review)
        self.assertIn("Total agent runs: 5", review)
        self.assertIn(
            "\nmodels:\ncdx --model gpt-5.5\ncld --model claude-sonnet-4-6\ncdx --model gpt-5.5\ncld --model claude-sonnet-4-6\nreviewer: cld --model claude-opus-4-8\n",
            review,
        )
        self.assertNotIn("prompt delivery", review)
        self.assertNotIn("command config", review)
        self.assertIn("Task:\nCheck auth and data loading.", review)
        self.assertIn("Operational instructions:", review)

    def test_latest_runtime_dir_finds_newest_across_work_dirs(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as raw_dir:
            project_dir = Path(raw_dir) / "project"
            older = project_dir / ".planning/work/sec-advisor/20260526-100000"
            newer = project_dir / ".planning/work/arch-advisor/20260526-110000"
            older.mkdir(parents=True)
            newer.mkdir(parents=True)
            (older / "session.json").write_text("{}")
            (newer / "session.json").write_text("{}")

            self.assertEqual(latest_runtime_dir(project_dir, ["sec-advisor", "arch-advisor"]), newer)


if __name__ == "__main__":
    unittest.main()
