import { randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { mkdir, readFile, realpath, rename, writeFile } from "node:fs/promises";
import { KeyperError } from "./errors.js";

const START = "<!-- keyper:start -->";
const END = "<!-- keyper:end -->";

export const AGENT_BLOCK = `${START}
- Treat every environment-variable value as a secret, regardless of its name.
- Never print, display, log, hash, encode, partially reveal, or place a secret in a command argument.
- Never directly read dotenv files or run provider commands that return values.
- Use \`keyper list\`, \`copy\`, \`copy-many\`, \`compare\`, \`audit\`, \`sync\`, \`move\`, \`remove\`, \`generate\`, \`compose\`, \`issue\`, \`put --stdin\`, \`enable\`, \`disable\`, \`plan\`, \`doctor\`, \`cloudflare-doctor\`, \`run\`, \`garmin\`, \`twilio\`, or \`livekit\` for secret work.
- To use a secret in a request, run a tool declared under \`tools\` in \`.keyper.json\` with \`keyper run TOOL -- ARGS\` (or a wrapper such as \`keyper garmin SUBCOMMAND\`). Keyper injects the values into that process's environment only and redacts them from its output. Do not read the value, pass it as an argument, or ask the user to run the command instead; if no suitable tool is declared, ask the user to add one.
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
  // Preserve shared instruction/settings symlinks instead of replacing the link.
  try { path = await realpath(path); } catch (error: unknown) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw new KeyperError("RULES_INSTALL_FAILED");
  }
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.keyper-${randomUUID()}`;
  try {
    await writeFile(temporary, content, { encoding: "utf8", mode: 0o600, flag: "wx" });
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
  const claudeRoot = target.global ? join(homedir(), ".claude") : root;
  const claudeInstructions = join(claudeRoot, "CLAUDE.md");
  const settingsPath = target.global
    ? join(claudeRoot, "settings.json") : join(root, ".claude", "settings.json");
  const claudeText = mergeAgentBlock(await readOptionalText(claudeInstructions));
  const settingsText = mergeClaudeSettings(await readOptionalText(settingsPath));
  await atomicTextWrite(agentsPath, mergeAgentBlock(existing));
  await atomicTextWrite(rulesPath, CODEX_RULES);
  await atomicTextWrite(claudeInstructions, claudeText);
  await atomicTextWrite(settingsPath, settingsText);
}

function mergeClaudeSettings(existing: string): string {
  // Settings may contain private configuration. Merge internally and never log it.
  try {
    const settings: unknown = JSON.parse(existing || "{}");
    if (!isObject(settings) || (settings.permissions !== undefined && !isObject(settings.permissions))) {
      throw new Error();
    }
    const permissions = isObject(settings.permissions) ? settings.permissions : {};
    if (permissions.deny !== undefined && (!Array.isArray(permissions.deny) || permissions.deny.some((rule: unknown) => typeof rule !== "string"))) {
      throw new Error();
    }
    permissions.deny = [...new Set([...(Array.isArray(permissions.deny) ? permissions.deny : []), ...CLAUDE_DENY_RULES])];
    settings.permissions = permissions;
    return `${JSON.stringify(settings, null, 2)}\n`;
  } catch {
    throw new KeyperError("RULES_INSTALL_FAILED");
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readOptionalText(path: string): Promise<string> {
  try { return await readFile(path, "utf8"); } catch (error: unknown) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return "";
    throw new KeyperError("RULES_INSTALL_FAILED");
  }
}

export const CLAUDE_DENY_RULES = [
  ...["vercel env", "convex env", "wrangler auth token", "wrangler@latest auth token"].flatMap(command =>
    ["", "npx ", "npx -y ", "npx --yes ", "npx --no-install ", "bunx ", "pnpm exec "].map(prefix => `Bash(${prefix}${command} *)`)
  ),
  "Bash(twilio *)", "Bash(lk *)"
];
