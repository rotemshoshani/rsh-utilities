# advisor

Unified advisor loop for repeated AI review passes.

It replaces the duplicated `sec-advisor` and `arch-advisor` runners with one
tmux controller and a launch wizard. The presets cover defensive security,
attacker-first vulnerability, architecture, and hygiene review tasks with their
own prompts, timings, work directories, and Codex-vs-Claude prompt delivery
behavior.

## Usage

```bash
./advisor run
```

The run command opens an `fzf`-style wizard:

- choose a topic: `sec`, `attack`, `arch`, `hygiene`, or `custom`
- for `custom`, enter the task description
- choose models from the generated list; each row shows `model | exact command`
- optionally choose one reviewer model, or `none`
- set cycle count and seconds per agent run
- review the final prompt, full command queue, run counts, timing, and
  output directory before tmux starts

Artifacts are written under the reviewed repo:

```bash
<project>/.planning/work/<topic-work-dir>/<YYYYMMDD-HHMMSS>/
```

For the built-in presets, that means:

```bash
<project>/.planning/work/sec-advisor/<timestamp>/
<project>/.planning/work/attack-advisor/<timestamp>/
<project>/.planning/work/arch-advisor/<timestamp>/
<project>/.planning/work/hygiene-advisor/<timestamp>/
```

The resolved run configuration is saved as:

```bash
<runtime-dir>/run-config.json
```

If a reviewer is selected, it runs once after all repeated advisor passes and
writes:

```bash
<runtime-dir>/final-review.html
```

## Models

`config.json` defines model-free commands:

- `codex`: `cdx`
- `pi`: `pi --approve --provider openrouter`
- `claude`: `cld`

It also pre-seeds model lists for:

- Codex/OpenAI: Codex default, GPT-5.6 Sol, GPT-5.6 Terra, GPT-5.6 Luna,
  GPT-5.5, GPT-5.4, GPT-5.4 mini
- OpenRouter: Z.ai GLM 5.3 Flash
- Claude: Claude default, Fable 5, Opus 5, Opus 4.8, Sonnet 4.6, Haiku 4.5

The wizard shows all configured model options for the selected topic and derives
the exact command from the base command plus the model attributes. For example,
selecting GPT-5.6 Sol under `codex` runs:

```bash
cdx --model gpt-5.6
```

Selecting GLM 5.3 Flash runs Pi with its built-in OpenRouter provider:

```bash
source <utilities>/.env.local
pi --approve --provider openrouter --extension <advisor>/pi_glm_5_3_flash.js \
  --model z-ai/glm-5.3-flash --thinking max @<runtime>/last-prompt.txt
```

Advisor sources the utilities repository's root `.env.local`, exports
`OPENROUTER_API_KEY`, and then launches Pi directly. Pi receives the prompt using
its documented native `@file` syntax in the default interactive TUI, so the Pi
interface, model turns, and tool activity remain visible. The key value is never
included in the generated command or resolved run configuration. When the agent
fully settles, the scoped extension shows a validation notification inside Pi
and writes a private completion file that Advisor checks every five
seconds. The same extension registers the newly released model, which is still
absent from Pi's refreshed bundled model catalog. Its thinking-level map exposes
the model's OpenRouter-supported `low`, `high`, and `max` levels; the attack
preset selects `max`.

The `attack` preset also treats both per-run markdown reports as required. Pi
becoming idle is not enough to complete a run: Advisor rejects missing, undersized,
or known-placeholder output. It sends a corrective follow-up into the same visible
Pi session and gives that turn a fresh work window. After four unsuccessful
continuations, it stops the queue and records `run-failure.json` instead of
silently accepting an empty run or consuming the next cycle. Reviewer completion
is likewise gated on a substantive `final-review.html` document.

The preview prints the full command queue in execution order, including repeated
cycles. If a reviewer is selected, its final command is shown once at the end.

Codex and Pi runs finish early when their completion signal is detected. Advisor
checks every five seconds and validates required artifacts before proceeding.
Other runs still use the configured sleep duration unless finished manually.

## Reviewer

The reviewer reads all reports, findings, plans, captures, and supporting files
under the run directory. It produces a single self-contained HTML summary that
explains the major findings and decisions in simple terms, accepts strong
findings, drops weak or duplicate findings, and proposes the clearest way
forward.

## Commands

```bash
./advisor run ~/projects/my-app
./advisor attach
./advisor stop-next
./advisor finish-sleep
./advisor kill
./advisor status
```

For non-interactive default runs:

```bash
./advisor run --no-tui --topic arch ~/projects/my-app
./advisor run --no-tui --topic hygiene ~/projects/my-app
```

For a cost-bounded attack canary, run one cycle with a ten-minute wall-clock
limit:

```bash
./advisor run --no-tui --topic attack --cycles 1 --seconds 600 ~/projects/my-app
```

When the time limit expires or required artifacts are incomplete, Advisor kills
the worker process before stopping so it cannot continue spending in the
background.

## Controller Keys

Focus the controller pane and press:

| Key | Action |
| --- | --- |
| `S` | Stop after the current run finishes |
| `F` | Finish the current sleep immediately |
| `Q` | Kill the tmux session now |
