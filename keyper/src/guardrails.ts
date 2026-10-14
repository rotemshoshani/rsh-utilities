import { randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { KeyperError } from "./errors.js";

const START = "<!-- keyper:start -->";
const END = "<!-- keyper:end -->";

export const AGENT_BLOCK = `${START}
- Treat every environment-variable value as a secret, regardless of its name.
- Never print, display, log, hash, encode, partially reveal, or place a secret in a command argument.
- Never directly read dotenv files or run provider commands that return values.
- Use \`keyper list\`, \`copy\`, \`copy-many\`, \`compare\`, \`audit\`, \`sync\`, \`move\`, \`remove\`, \`generate\`, \`compose\`, \`issue\`, \`put --stdin\`, \`enable\`, \`disable\`, \`plan\`, \`doctor\`, \`cloudflare-doctor\`, \`twilio\`, or \`livekit\` for secret work.
- Listing secret names is allowed. Do not use a \`get\`, \`show\`, \`export\`, raw-output, or debug workaround.
- If \`keyper\` cannot perform an operation safely, stop and report the sanitized error instead of bypassing it.
${END}`;

export const CODEX_RULES = `# Managed by keyper. Restart Codex after changes.

prefix_rule(
    pattern = ["vercel", "env", ["pull", "run", "add", "update", "remove", "rm"]],
    decision = "forbidden",
    justification = "Use keyper so environment-variable values cannot enter agent output or command history.",
)

prefix_rule(
    pattern = ["npx", "vercel", "env", ["pull", "run", "add", "update", "remove", "rm"]],
    decision = "forbidden",
    justification = "Use keyper so environment-variable values cannot enter agent output or command history.",
)

prefix_rule(
    pattern = ["npx", ["-y", "--yes"], "vercel", "env", ["pull", "run", "add", "update", "remove", "rm"]],
    decision = "forbidden",
    justification = "Use keyper so environment-variable values cannot enter agent output or command history.",
)

prefix_rule(
    pattern = ["convex", "env", ["get", "list", "set", "remove", "rm"]],
    decision = "forbidden",
    justification = "Use keyper; raw Convex env list and get commands can reveal values.",
)

prefix_rule(
    pattern = ["npx", "convex", "env", ["get", "list", "set", "remove", "rm"]],
    decision = "forbidden",
    justification = "Use keyper; raw Convex env list and get commands can reveal values.",
)

prefix_rule(
    pattern = ["npx", "--no-install", "convex", "env", ["get", "list", "set", "remove", "rm"]],
    decision = "forbidden",
    justification = "Use keyper; raw Convex env list and get commands can reveal values.",
)

prefix_rule(
    pattern = ["npx", ["-y", "--yes"], "convex", "env", ["get", "list", "set", "remove", "rm"]],
    decision = "forbidden",
    justification = "Use keyper; raw Convex env list and get commands can reveal values.",
)

prefix_rule(
    pattern = ["bunx", "convex", "env", ["get", "list", "set", "remove", "rm"]],
    decision = "forbidden",
    justification = "Use keyper; raw Convex env list and get commands can reveal values.",
)

prefix_rule(
    pattern = ["pnpm", "exec", "convex", "env", ["get", "list", "set", "remove", "rm"]],
    decision = "forbidden",
    justification = "Use keyper; raw Convex env list and get commands can reveal values.",
)

prefix_rule(
    pattern = ["wrangler", "auth", "token"],
    decision = "forbidden",
    justification = "Use keyper; raw Wrangler auth token commands reveal a credential.",
)

prefix_rule(
    pattern = ["npx", ["wrangler", "wrangler@latest"], "auth", "token"],
    decision = "forbidden",
    justification = "Use keyper; raw Wrangler auth token commands reveal a credential.",
)

prefix_rule(
    pattern = ["npx", ["-y", "--yes"], ["wrangler", "wrangler@latest"], "auth", "token"],
    decision = "forbidden",
    justification = "Use keyper; raw Wrangler auth token commands reveal a credential.",
)

prefix_rule(
    pattern = ["bunx", ["wrangler", "wrangler@latest"], "auth", "token"],
    decision = "forbidden",
    justification = "Use keyper; raw Wrangler auth token commands reveal a credential.",
)

prefix_rule(
    pattern = ["pnpm", "exec", ["wrangler", "wrangler@latest"], "auth", "token"],
    decision = "forbidden",
    justification = "Use keyper; raw Wrangler auth token commands reveal a credential.",
)

prefix_rule(
    pattern = [["twilio", "lk"]],
    decision = "forbidden",
    justification = "Use keyper twilio or keyper livekit so provider credentials and private routing data cannot enter agent output.",
)
`;

async function atomicTextWrite(path: string, content: string): Promise<void> {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.keyper-${randomUUID()}`;
  try {
    await writeFile(temporary, content, { encoding: "utf8", mode: 0o644, flag: "wx" });
    await rename(temporary, path);
  } catch {
    throw new KeyperError("RULES_INSTALL_FAILED");
  }
}

function mergeAgentBlock(existing: string): string {
  const start = existing.indexOf(START);
  const end = existing.indexOf(END);
  if ((start === -1) !== (end === -1) || (end !== -1 && end < start)) {
    throw new KeyperError("RULES_INSTALL_FAILED");
  }
  if (start !== -1 && end !== -1) {
    return `${existing.slice(0, start)}${AGENT_BLOCK}${existing.slice(end + END.length)}`;
  }
  const prefix = existing.trimEnd();
  return `${prefix ? `${prefix}\n\n` : ""}# Secret handling\n\n${AGENT_BLOCK}\n`;
}

export async function installGuardrails(target: { global: boolean; project?: string }): Promise<void> {
  const root = target.global ? join(homedir(), ".codex") : resolve(target.project ?? ".");
  const agentsPath = join(root, "AGENTS.md");
  const rulesPath = target.global
    ? join(root, "rules", "keyper.rules")
    : join(root, ".codex", "rules", "keyper.rules");
  let existing = "";
  try {
    existing = await readFile(agentsPath, "utf8");
  } catch (error: unknown) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
      throw new KeyperError("RULES_INSTALL_FAILED");
    }
  }
  await atomicTextWrite(agentsPath, mergeAgentBlock(existing));
  await atomicTextWrite(rulesPath, CODEX_RULES);
}
