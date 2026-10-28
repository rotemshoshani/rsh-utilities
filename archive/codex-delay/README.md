# codex-delay

`codex-delay` schedules one prompt for Codex from the repository you are
currently in. It opens `micro` first for the prompt, then presents one dashboard
where all three schedule inputs remain visible and editable:

1. the prompt, edited in a temporary Markdown file using `micro`,
2. a delay (`90`, `01:30`, or `00:01:30`), and
3. a fresh thread or one of the resumable Codex threads recorded for the exact
   current working directory.

Run it from the target repository:

```bash
$HOME/projects/utilities/codex-delay/codex-delay
```

The session picker reads Codex's local read-only state database, its
`session_index.jsonl`, and the first metadata record in each saved rollout. It
shows the explicit thread name and Codex-generated title separately, along with
updated time, thread ID, cwd, source, Git branch, model, reasoning effort, token
usage, and CLI version when available. Subagent,
archived, and non-interactive rollouts are omitted, matching the useful default
behavior of `codex resume`.

After collection, the tool runs the repository's `slp/slp` countdown in the
foreground. When it reaches zero it starts an interactive Bash shell so the
local `cdx` alias and `codex` shell function are loaded, then executes one of:

```bash
cdx resume <thread-id> '<prompt>'
cdx '<prompt>'
```

Use `--dry-run` to exercise the complete UI and print both shell commands
without waiting or launching Codex:

```bash
./codex-delay --dry-run
```

The prompt file lasts for the entire dashboard session. Enter or `e` while the
Prompt section is focused reopens that same file in `micro`, preserving edits.
Use Tab/Shift-Tab to move between sections, type the delay directly, and use
arrow keys or `j`/`k` in the session list. Press `s` or F5 to schedule, and
`q`/Esc to cancel.

## Optional shortcut

The wrapper runs directly with the system Python and has no third-party
dependencies. If you want a short command, add this to `~/.bashrc`:

```bash
alias cdx-later="$HOME/projects/utilities/codex-delay/codex-delay"
```

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
