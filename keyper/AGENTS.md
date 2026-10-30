# Secret handling

<!-- keyper:start -->
- Treat every environment-variable value as a secret, regardless of its name.
- Never print, display, log, hash, encode, partially reveal, or place a secret in a command argument.
- Never directly read dotenv files or run provider commands that return values.
- Use `keyper list`, `copy`, `copy-many`, `compare`, `audit`, `sync`, `move`, `remove`, `generate`, `compose`, `issue`, `put --stdin`, `enable`, `disable`, `plan`, `doctor`, `cloudflare-doctor`, `run`, `garmin`, `twilio`, or `livekit` for secret work.
- To use a secret in a request, run a tool declared under `tools` in `.keyper.json` with `keyper run TOOL -- ARGS` (or a wrapper such as `keyper garmin SUBCOMMAND`). Keyper injects the values into that process's environment only and redacts them from its output. Do not read the value, pass it as an argument, or ask the user to run the command instead; if no suitable tool is declared, ask the user to add one.
- Listing secret names is allowed. Do not use a `get`, `show`, `export`, raw-output, or debug workaround.
- If `keyper` cannot perform an operation safely, stop and report the sanitized error instead of bypassing it.
<!-- keyper:end -->
