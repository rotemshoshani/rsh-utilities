import { lstat, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { CLAUDE_DENY_RULES, CODEX_RULES, installGuardrails } from "../src/guardrails.js";
import { runSafeProcess } from "../src/safe-process.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  while (temporaryDirectories.length > 0) {
    await rm(temporaryDirectories.pop()!, { recursive: true, force: true });
  }
});

describe("installGuardrails", () => {
  it("merges Claude rules without replacing shared instructions or unrelated settings", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-claude-rules-test-"));
    temporaryDirectories.push(directory);
    await writeFile(join(directory, "AGENTS.md"), "# Shared instructions\n");
    await symlink("AGENTS.md", join(directory, "CLAUDE.md"));
    await mkdir(join(directory, ".claude"));
    await writeFile(join(directory, ".claude", "settings.json"), JSON.stringify({
      hooks: { Stop: [] }, permissions: { allow: ["Bash(npm test)"], deny: ["Bash(custom *)"] }
    }));
    await installGuardrails({ global: false, project: directory });
    await installGuardrails({ global: false, project: directory });
    expect((await lstat(join(directory, "CLAUDE.md"))).isSymbolicLink()).toBe(true);
    const settings = JSON.parse(await readFile(join(directory, ".claude", "settings.json"), "utf8"));
    expect(settings.hooks).toEqual({ Stop: [] });
    expect(settings.permissions.allow).toEqual(["Bash(npm test)"]);
    expect(settings.permissions.deny).toEqual(["Bash(custom *)", ...CLAUDE_DENY_RULES]);
    const instructions = await readFile(join(directory, "CLAUDE.md"), "utf8");
    expect(instructions.match(/<!-- keyper:start -->/g)).toHaveLength(1);
  });

  it("rejects malformed Claude settings without replacing them", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-invalid-rules-test-"));
    temporaryDirectories.push(directory);
    await mkdir(join(directory, ".claude"));
    const path = join(directory, ".claude", "settings.json");
    await writeFile(path, '{"permissions":{"deny":"invalid"}}');
    await expect(installGuardrails({ global: false, project: directory })).rejects.toMatchObject({ code: "RULES_INSTALL_FAILED" });
    expect(await readFile(path, "utf8")).toBe('{"permissions":{"deny":"invalid"}}');
  });

  it("preserves unrelated instructions and updates its managed block", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-rules-test-"));
    temporaryDirectories.push(directory);
    await writeFile(join(directory, "AGENTS.md"), "# Existing\n\n- Keep this.\n");
    await installGuardrails({ global: false, project: directory });
    await installGuardrails({ global: false, project: directory });
    const agents = await readFile(join(directory, "AGENTS.md"), "utf8");
    const rules = await readFile(join(directory, ".codex", "rules", "keyper.rules"), "utf8");
    expect(agents.includes("- Keep this.")).toBe(true);
    expect(agents.match(/<!-- keyper:start -->/g)?.length).toBe(1);
    expect(rules.includes('decision = "forbidden"')).toBe(true);
    const trackedRules = await readFile(resolve(".codex/rules/keyper.rules"), "utf8");
    expect(trackedRules === CODEX_RULES).toBe(true);
    const policy = await runSafeProcess("codex", [
      "execpolicy",
      "check",
      "--rules",
      join(directory, ".codex", "rules", "keyper.rules"),
      "--",
      "vercel",
      "env",
      "pull"
    ]);
    try {
      expect(policy.includes(Buffer.from('"forbidden"'))).toBe(true);
    } finally {
      policy.fill(0);
    }
    for (const command of [
      ["wrangler", "auth", "token", "--json"],
      ["npx", "--yes", "wrangler@latest", "auth", "token", "--json"],
      ["bunx", "wrangler", "auth", "token", "--json"],
      ["pnpm", "exec", "wrangler", "auth", "token", "--json"]
    ]) {
      const result = await runSafeProcess("codex", ["execpolicy", "check", "--rules", join(directory, ".codex", "rules", "keyper.rules"), "--", ...command]);
      try {
        expect(result.includes(Buffer.from('"forbidden"'))).toBe(true);
      } finally {
        result.fill(0);
      }
    }
    for (const command of [["twilio", "profiles:list"], ["lk", "project", "list", "--json"]]) {
      const result = await runSafeProcess("codex", ["execpolicy", "check", "--rules", join(directory, ".codex", "rules", "keyper.rules"), "--", ...command]);
      try {
        expect(result.includes(Buffer.from('"forbidden"'))).toBe(true);
      } finally {
        result.fill(0);
      }
    }
  });
});
