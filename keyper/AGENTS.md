# Secret handling

<!-- keyper:start -->
- Treat every environment-variable value as a secret, regardless of its name.
- Never print, display, log, hash, encode, partially reveal, or place a secret in a command argument.
- Never directly read dotenv files or run provider commands that return values.
- Use `keyper list`, `copy`, `copy-many`, `compare`, `audit`, `sync`, `move`, `remove`, `generate`, `compose`, `issue`, `put --stdin`, `enable`, `disable`, `plan`, `doctor`, `cloudflare-doctor`, `twilio`, or `livekit` for secret work.
- Listing secret names is allowed. Do not use a `get`, `show`, `export`, raw-output, or debug workaround.
- If `keyper` cannot perform an operation safely, stop and report the sanitized error instead of bypassing it.
<!-- keyper:end -->
