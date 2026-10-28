# prompt-queue-plan Skill Setup

This project pairs `prompt-queue` with a Codex skill named `prompt-queue-plan`.
The skill is meant for a planning chat: after discussing a plan, invoke the
skill to turn that plan into queued Codex executor prompts.

## Local Skill Path

Install the skill at:

```bash
~/.agents/skills/prompt-queue-plan/SKILL.md
```

This repo includes the skill template at:

```bash
prompt-queue/skill/prompt-queue-plan/SKILL.md
```

Install it with:

```bash
mkdir -p ~/.agents/skills/prompt-queue-plan
cp prompt-queue/skill/prompt-queue-plan/SKILL.md ~/.agents/skills/prompt-queue-plan/SKILL.md
```

After adding or changing skills, start a fresh Codex chat so the skill list is
reloaded.

## Trigger

Use one of:

```text
$prompt-queue-plan
$queue-plan
```

## What The Skill Does

The skill should:

- infer the target repo from the planning chat
- choose a safe, descriptive queue tag
- create a self-contained `prompt-queue/queues/<tag>/` folder
- write the target repo to `prompt-queue/queues/<tag>/.env.local`
- write ordered prompt files under `prompt-queue/queues/<tag>/prompts/`
- write `prompt-queue/queues/<tag>/config.json`
- configure `cdx`, `argument_file`, `Ready` detection, and `DO-NOT-PROCEED` blocking
- append the blocking instruction to every executor prompt
- validate the JSON
- report both `/absolute/path/to/prompt-queue run <tag>` and `pq <tag>`

The skill should not run the tmux queue unless explicitly asked.

## Blocking Contract

Every generated executor prompt must end with this exact instruction:

```text
If something is blocking you from completing this prompt safely, write exactly DO-NOT-PROCEED on its own final line at the very end of your output.
```

The runner detects `Ready`, then scans recent output for an exact line:

```text
DO-NOT-PROCEED
```

If found, the queue captures the pane, writes `blocked.json`, leaves the worker
pane intact, and does not proceed to the next prompt.

## Ask Codex To Create This Skill

In another environment, give Codex this prompt:

```text
Create a Codex skill named prompt-queue-plan.

The skill should trigger when I say $prompt-queue-plan, $queue-plan, ask to
compile a plan into queued Codex prompts, or ask to write prompt-queue config
from the current planning chat.

The skill must only prepare prompt-queue files. It must not implement the plan
in the target repo and must not start tmux unless explicitly asked.

It should write files in the `prompt-queue` project directory in this utilities
repo. If the path is unclear, ask the user for the utilities repo path before
writing files.

Workflow:
1. Infer the target repo from the planning chat; ask one concise question if unclear.
2. Break the plan into ordered, self-contained executor prompts.
3. Choose a queue tag containing only letters, numbers, hyphens, and underscores.
4. Create prompt-queue/queues/<tag>/prompts/ and write one markdown file per prompt there.
5. Write prompt-queue/queues/<tag>/.env.local with PROMPT_QUEUE_WORKDIR set to the target
   repo absolute path.
6. Write prompt-queue/queues/<tag>/config.json with project_dir "${PROMPT_QUEUE_WORKDIR}",
   command "cdx", prompt_delivery "argument_file", run_seconds 2700,
   ready_check_seconds 60, ready_check_lines 1, ready_markers ["Ready"],
   block_marker "DO-NOT-PROCEED", block_check_lines 10, prompts file
   references, and prompt_files [].
7. Validate JSON with python3 -m json.tool.
8. Report the tag, prompt count, target repo, queue folder, env path, config
   path, absolute runner command with the tag, and pq alias command with the tag.

Every executor prompt must include target repo path, planning files to read,
exact scope, constraints, non-goals, verification commands, and an instruction
not to commit unless explicitly requested.

Append this exact instruction to the end of every generated executor prompt:
If something is blocking you from completing this prompt safely, write exactly DO-NOT-PROCEED on its own final line at the very end of your output.
```
