# Keyper

Keyper manages secret values across local/remote environment files, structured JSON, Convex, and Vercel without printing their values. It exists so people and coding agents can perform routine secret operations without putting credentials in terminal output, command arguments, logs, or chat history.

Keyper can list names and portability status, but it has no command that displays a value. When a task needs a secret *used*—for example, an API call—`keyper run` starts an allowlisted tool with the value in that process's environment and redacts it from the tool's output. That is the sanctioned way for agents to use a secret.

## Setup

Requirements: Node.js 22+ and the authenticated provider CLIs used by your commands. Twilio wrappers require `twilio`; LiveKit wrappers require `lk`.

Vercel writes require a CLI version with `vercel api --input` support. Keyper uses the CLI's current login and token refresh for reads and writes. Request values travel through stdin, with response output suppressed and captured buffers cleared. An explicit `VERCEL_TOKEN` override is passed through an owner-only temporary CLI configuration, never an argument.

```sh
npm install
npm run build
npm link
```

Create the configuration:

```sh
cp .keyper.example.json .keyper.json
```

Without `--config`, Keyper uses the nearest `.keyper.json` in the current directory or any parent, then `~/.config/keyper/config.json` (`$XDG_CONFIG_HOME/keyper/config.json` when set). A `CONFIG_INVALID` error names the paths it tried. Relative paths in a configuration resolve against the directory that contains it.

Define aliases for the locations you use. The file contains provider locations only—never credentials or secret values.

```json
{
  "version": 1,
  "aliases": {
    "local": { "provider": "local", "path": ".env.local" },
    "convex-dev": {
      "provider": "convex",
      "deployment": "team:project:dev/deployment-name"
    },
    "vercel-preview": {
      "provider": "vercel",
      "project": "my-app",
      "environment": "preview"
    },
    "app-config": {
      "provider": "structured",
      "path": "config.json",
      "fields": {
        "API_KEY": "/services/api/key",
        "FEATURE_ENABLED": "/features/example/enabled"
      }
    },
    "remote-env": {
      "provider": "ssh",
      "host": "production-app",
      "path": "/etc/my-app/environment",
      "format": "systemd"
    },
    "remote-config": {
      "provider": "ssh-structured",
      "host": "production-app",
      "path": "/etc/my-app/config.json",
      "fields": { "API_KEY": "/services/api/key" }
    },
    "wrangler-auth": { "provider": "wrangler" }
  },
  "issuers": {
    "access-token": {
      "provider": "cloudflare-access-service-token",
      "accountId": "local:CLOUDFLARE_ACCOUNT_ID",
      "apiToken": "local:CLOUDFLARE_API_TOKEN",
      "tokenName": "service-token"
    }
  }
}
```

Declare the tools that may use secrets (see [Using a secret](#using-a-secret)):

```json
{
  "tools": {
    "garmin": {
      "command": ["python3", ".codex/scripts/garmin_api.py"],
      "env": { "GARMIN_API_KEY": "local:GARMIN_API_KEY" }
    }
  }
}
```

Check connectivity, then list the available names:

```sh
keyper doctor
keyper list --all
```

## Commands

```sh
# List names only
keyper list --all
keyper list --source local --json

# Copy and internally verify a value
keyper copy API_KEY --from local --to convex-dev

# Compare two values without displaying them
keyper compare API_KEY --between local convex-dev
keyper compare local:OLD_NAME convex-dev:NEW_NAME

# Audit reusable equality checks declared in .keyper.json
keyper audit production-api
keyper audit --all --json

# Repair a check from an explicit canonical source
keyper sync production-api --from local:API_KEY --overwrite

# Generate, compose, or accept a value through stdin
keyper generate SESSION_SECRET --to local --to convex-dev
keyper compose TRUSTED_KEYS --from local:FIRST_KEY --from local:SECOND_KEY \
  --format json --to convex-dev
secret-producing-command | keyper put API_KEY --stdin --to local

# Copy once and transactionally fan out
keyper copy-many API_KEY --from local --to convex-dev --to vercel-preview

# Write fixed booleans without accepting a value
keyper enable FEATURE_ENABLED --to app-config --overwrite
keyper disable FEATURE_ENABLED --to app-config --overwrite

# Print names and operations without reading or writing secret values
keyper plan copy-many API_KEY --from local --to convex-dev:RENAMED --to remote-env

# Add provider locations without opening the Keyper config
keyper alias-add remote-env --provider ssh --host production-app \
  --path /etc/my-app/environment --format systemd
keyper alias-add wrangler-auth --provider wrangler

# Capture every one-time provider output directly into destinations
keyper issue access-token \
  --to CLIENT_ID=convex-dev:ACCESS_CLIENT_ID \
  --to CLIENT_SECRET=convex-dev:ACCESS_CLIENT_SECRET \
  --to RESOURCE_ID=remote-env:ACCESS_TOKEN_RESOURCE_ID

# Move, verify, then delete the source
keyper move API_KEY --from local --to convex-dev \
  --confirm-delete-source API_KEY

# Delete without reading or displaying the value
keyper remove RETIRED_KEY --from vercel-preview \
  --confirm-delete RETIRED_KEY

# Use a secret: run a declared tool with values in its environment only
keyper run garmin -- activities --start 2026-10-27
keyper garmin activities --start 2026-10-27

# Install agent safety rules
keyper install-rules --project /path/to/repository
keyper install-rules --global

# List allowlisted provider metadata as JSON
keyper twilio list profiles
keyper twilio list accounts --profile production
keyper twilio list trunks --profile production
keyper twilio list numbers --profile production
keyper twilio list routing --profile production
keyper livekit list projects
keyper livekit list trunks --project production
keyper livekit list numbers --project production
keyper livekit list routing --project production

# Switch the provider default without exposing its command output
keyper twilio use production
keyper livekit use production

# Import a selected credential set directly into an atomic dotenv update
keyper twilio import production --to .env.local
keyper livekit import production --to .env.local --overwrite
```

Useful options:

- `--config PATH` uses a different configuration file.
- `--to-name NAME` renames a variable at the destination.
- `--overwrite` permits replacing an existing destination.
- `--sensitive` creates a locked Vercel Sensitive variable.
- `--accept-unverified-source-deletion` is required when moving into locked Vercel storage.
- Repeat `--to ALIAS[:NAME]` for transactional fan-out and per-destination renames.
- `generate` creates 32 random bytes encoded with base64url by default; use `--bytes 16..4096` to change the entropy size.
- `compose --format csv|json` supports only those fixed encodings.

Structured aliases expose only the configured JSON pointers as Keyper names. Writes preserve unrelated JSON fields and atomically replace the file with mode `0600`. SSH aliases use the configured host and absolute path as an allowlist, require `ssh` locally and `python3` remotely, and atomically replace dotenv/systemd environment or structured JSON files with mode `0600`.

Provider-issued credentials are captured once, written to every required destination, and revoked at the provider when fan-out fails. The Cloudflare Access issuer consumes its account ID and API token through Keyper source references and never places either value in an HTTP URL, command argument, or output.

Reusable audits compare exact bytes without displaying or deriving output from the values:

```json
{
  "checks": {
    "production-api": ["local:API_KEY", "convex-dev:API_KEY", "vercel-preview:API_KEY"]
  }
}
```

An audit reports `equal`, `different`, `missing`, `locked`, or `failed`. `sync` repairs a check from an explicitly selected member using transactional fan-out. Locked values fail because equality cannot be verified and must be recreated with an explicit sensitive copy.

## Using a secret

`keyper run TOOL [--config PATH] [-- ARGS...]` runs a tool declared under `tools`:

- `command` is the program and its fixed arguments. Keyper runs it directly, never through a shell, from the configuration's directory (or `cwd`, relative to it). Arguments after `--` pass through verbatim; Keyper does not parse them.
- `env` maps child environment-variable names to `ALIAS:NAME` references. Values are read inside Keyper and placed only in the child's environment block—never in arguments, a shell, a temporary file, or a log. Names that steer code loading (`PATH`, `LD_*`, `NODE_OPTIONS`, `PYTHONPATH`, …) are rejected.
- Optional `timeoutSeconds` (default 120, max 3600) and `maxOutputBytes` (stdout plus stderr, default 4 MiB, max 16 MiB).

Before running, Keyper refuses a tool whose command can only reveal what it was given: environment and file printers (`env`, `printenv`, `set`, `cat`, `sed`, `awk`, `grep`, `head`, `xxd`, `base64`, `jq`, …), launchers (`xargs`, `sudo`, `strace`, `docker`, …), shells, interpreters without a declared script or with inline code (`python3 -c`, `node -e`, …), and any declared or passed argument that names an env file, `/proc/*/environ`, or a configured local/structured secret file.

Keyper buffers the tool's complete stdout and stderr, then replaces every occurrence of each injected value with `[redacted]`: the exact value, its trimmed and CRLF/LF variants, each line of a multi-line value, JSON-escaped, URL-encoded (`%XX` either case, `+` for spaces), hex (either case), and base64/base64url with or without padding—including base64 embedded in a larger payload such as a Basic-auth header. Only after both streams are scrubbed does Keyper print them, followed by one stderr line naming the tool, the injected variable names, the redaction count, and the exit code. The command exits with the tool's exit code.

It fails closed with a sanitized error and no tool output when the output is not UTF-8 text or contains NUL (binary), exceeds `maxOutputBytes`, or the tool times out. Stdin is not connected, so interactive tools are unsupported.

`keyper garmin SUBCOMMAND [ARGS...]` is shorthand for `keyper run garmin -- SUBCOMMAND [ARGS...]`.

Limits: redaction covers the injected values in the encodings above, not arbitrary transformations (for example encryption, hashing, or a value interleaved with other characters); a base64 character that straddles the value and adjacent bytes can remain. A declared tool is trusted code: Keyper keeps values out of the agent's transcript, but cannot stop a tool from writing them to a file or sending them over the network. Output appears only after the tool exits.

## Safety model

Values stay inside Keyper processes and are transferred through private buffers or stdin; `keyper run` additionally passes them to a declared tool's environment. Output contains only names, aliases, operations, portability, status, and sanitized error codes. A normal move deletes its source only after reading the destination and confirming equality internally.

Fan-out commands preflight all destinations and snapshot readable existing values. If a later write fails, Keyper rolls back completed writes where possible and reports only `rolled-back`, `rollback-failed`, `failed`, and `not-attempted` statuses. An overwritten locked value cannot be restored because it cannot be read.

Vercel Sensitive values become non-readable after creation. They can be written but cannot later be copied out or compared, so keep another canonical copy. Moving into Sensitive storage requires explicit acknowledgement before Keyper deletes the source.

`install-rules` installs safety guidance for both Codex and Claude Code. It updates managed blocks in `AGENTS.md` and `CLAUDE.md`, writes Codex command rules, and merges Claude Bash deny rules into `.claude/settings.json` (or the user settings for `--global`). Existing instructions, settings, and symlinks are preserved. Restart existing agent sessions after installation. Permission-bypass launch modes can disable harness enforcement; the guidance still directs secret operations through Keyper.

Twilio uses explicit TSV columns because its JSON mode ignores `--properties`. LiveKit JSON is validated against a field-by-field schema. Known credential, authentication, metadata, header, PIN, and pagination-token fields are dropped; any unknown field aborts the command before output. Imports accept only a target named `.env.local` and atomically write the selected credentials without returning their values.

`keyper doctor` checks provider connectivity and, for every `local` and `structured` alias, reports whether its file is `ignored`, `tracked`, `untracked-not-ignored`, `not-in-repo`, or `unknown` according to git (`git check-ignore`, `git ls-files`). `tracked` and `untracked-not-ignored` print a warning. It also reports each tool as `ok`, `unsafe`, `missing` (a referenced name is absent), `locked`, or `failed`. Only names and statuses are printed. `--json` returns `{ "aliases", "files", "tools" }`. Exit status: 5 when an alias fails or a tool is not `ok`, 4 when only file warnings remain, otherwise 0.

## Matrix test

```sh
npm run test:matrix
```

The matrix test uses disposable real resources. It runs `copy`, `compare`, and verified `move` through all nine directed routes among local, Convex, and Vercel, plus Vercel portable/Sensitive behavior and a disposable deployment. Use `KEYPER_MATRIX_GROUP=routes1` (or another documented group in the test file) to run a bounded slice.
