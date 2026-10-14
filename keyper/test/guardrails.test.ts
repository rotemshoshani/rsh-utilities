import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { CODEX_RULES, installGuardrails } from "../src/guardrails.js";
import { runSafeProcess } from "../src/safe-process.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  while (temporaryDirectories.length > 0) {
    await rm(temporaryDirectories.pop()!, { recursive: true, force: true });
  }
});

describe("installGuardrails", () => {
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
