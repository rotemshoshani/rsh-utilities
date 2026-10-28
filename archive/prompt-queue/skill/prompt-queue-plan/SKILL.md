---
name: prompt-queue-plan
description: Turn a planning conversation into a runnable prompt-queue config. Use when the user says $prompt-queue-plan, $queue-plan, asks to compile a plan into queued Codex prompts, or wants to write prompt-queue prompt files/config from the current chat.
---

# Prompt Queue Plan

Convert the current planning conversation into a tagged `prompt-queue` run.
Write one self-contained queue folder in this utilities repo's `prompt-queue`
project directory. If the path is unclear, ask the user for the utilities repo
path before writing files.

Do not implement the plan in the target repo. Only prepare the queued executor prompts and config.

## Workflow

1. Infer the target repo from the conversation. If it is unclear, ask one concise question.
2. Choose a short descriptive queue tag using only letters, numbers, hyphens,
   and underscores. The tag is the queue id and folder name. Do not overwrite an
   existing `prompt-queue/queues/<tag>/`; choose a distinct tag or ask the user.
3. Create `prompt-queue/queues/<tag>/prompts/`. Keep every file for this queue
   inside `prompt-queue/queues/<tag>/`.
4. Break the plan into ordered, self-contained executor prompts. Prefer small phases over broad prompts.
5. For each prompt, write one markdown file under
   `prompt-queue/queues/<tag>/prompts/`.

Use deterministic names such as `001-phase-name.md`, `002-phase-name.md`.

6. Write `prompt-queue/queues/<tag>/.env.local` so:
   - `PROMPT_QUEUE_WORKDIR` is the target repo absolute path.
   - `CODEX_THREAD_ID` is the current Codex session id, when available.
7. Write `prompt-queue/queues/<tag>/config.json` so:
   - `project_dir` is `"${PROMPT_QUEUE_WORKDIR}"`.
   - `command` is `cdx`.
   - `prompt_delivery` is `argument_file`.
   - `ready_check_seconds` is `60`.
   - `ready_check_lines` is `1`.
   - `ready_markers` is `["Ready"]`.
   - `block_marker` is `"DO-NOT-PROCEED"`.
   - `block_check_lines` is `10`.
   - `blocked_recovery` is `true` when the current Codex session id is available.
   - `blocked_recovery_session_id` is `"${CODEX_THREAD_ID}"`.
   - `blocked_recovery_command` is `cdx`.
   - `blocked_recovery_success_marker` is `"PROCEED-ALLOWED"`.
   - `blocked_recovery_human_marker` is `"HUMAN-DECISION-REQUIRED"`.
   - `blocked_recovery_action` is `"retry"`.
   - `blocked_recovery_max_attempts` is `1`.
   - `blocked_recovery_check_lines` is `20`.
   - `completion_notify` is `true` when the current Codex session id is available.
   - `completion_notify_session_id` is `"${CODEX_THREAD_ID}"`.
   - `completion_notify_command` is `cdx`.
   - `completion_notify_check_lines` is `20`.
   - `prompts` is `[]`.
   - `prompt_files` references the files you wrote, in order, as paths relative
     to that queue's `config.json` such as `"prompts/001-phase-name.md"`.
     Do not put prompt file paths in `prompts`; this local runner treats string
     entries in `prompts` as literal inline prompt text.
8. Validate the JSON with `python3 -m json.tool`.
9. Report the queue tag, prompt count, target repo, queue folder, env path, and
   config path. Print both runnable commands with the tag substituted:
   - the absolute runner command: `/absolute/path/to/prompt-queue/prompt-queue run <tag>`
   - the alias form: `pq <tag>`

The runner automatically moves the whole completed queue folder to
`prompt-queue/archive/<timestamp>-<tag>/`. Do not archive it while preparing the
plan.

If `CODEX_THREAD_ID` is unavailable, set `blocked_recovery` and
`completion_notify` to `false` and omit `CODEX_THREAD_ID` from `.env.local`
instead of guessing.

Do not start the tmux queue unless the user explicitly asks you to run it.

## Executor Prompt Requirements

Every generated executor prompt must be self-contained. Include:

- target repo path
- planning files or artifacts to read
- exact scope
- explicit constraints and non-goals
- verification commands
- instruction not to commit unless explicitly requested

Append this exact instruction to the end of every executor prompt:

```text
If something is blocking you from completing this prompt safely, write exactly DO-NOT-PROCEED on its own final line at the very end of your output.
```

This marker is load-bearing. Do not paraphrase it. Do not add text after it inside the generated prompt.

## Prompt Quality Rules

- Keep each prompt narrow enough for one fresh Codex session.
- Include "do not drift" constraints when phases overlap.
- Prefer file references over relying on chat memory.
- If a phase depends on prior phases, state that dependency and tell the executor to inspect the current worktree first.
- If the plan is too ambiguous to queue safely, ask for clarification instead of guessing.
