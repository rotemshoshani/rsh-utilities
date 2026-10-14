# Keyper

Keyper manages secret values across local/remote environment files, structured JSON, Convex, and Vercel without printing their values. It exists so people and coding agents can perform routine secret operations without putting credentials in terminal output, command arguments, logs, or chat history.

Keyper can list names and portability status, but it has no command that displays a value.

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

## Safety model

Values stay inside Keyper processes and are transferred through private buffers or stdin. Output contains only names, aliases, operations, portability, status, and sanitized error codes. A normal move deletes its source only after reading the destination and confirming equality internally.

Fan-out commands preflight all destinations and snapshot readable existing values. If a later write fails, Keyper rolls back completed writes where possible and reports only `rolled-back`, `rollback-failed`, `failed`, and `not-attempted` statuses. An overwritten locked value cannot be restored because it cannot be read.

Vercel Sensitive values become non-readable after creation. They can be written but cannot later be copied out or compared, so keep another canonical copy. Moving into Sensitive storage requires explicit acknowledgement before Keyper deletes the source.

`install-rules` adds Codex command rules and an `AGENTS.md` block that direct agents to use Keyper instead of raw secret-bearing commands.

Twilio uses explicit TSV columns because its JSON mode ignores `--properties`. LiveKit JSON is validated against a field-by-field schema. Known credential, authentication, metadata, header, PIN, and pagination-token fields are dropped; any unknown field aborts the command before output. Imports accept only a target named `.env.local` and atomically write the selected credentials without returning their values.

## Matrix test

```sh
npm run test:matrix
```

The matrix test uses disposable real resources. It runs `copy`, `compare`, and verified `move` through all nine directed routes among local, Convex, and Vercel, plus Vercel portable/Sensitive behavior and a disposable deployment. Use `KEYPER_MATRIX_GROUP=routes1` (or another documented group in the test file) to run a bounded slice.
