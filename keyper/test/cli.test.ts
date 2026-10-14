import { randomBytes, timingSafeEqual } from "node:crypto";
import { spawn } from "node:child_process";
import { mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { LocalProvider } from "../src/providers/local.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  while (temporaryDirectories.length > 0) {
    await rm(temporaryDirectories.pop()!, { recursive: true, force: true });
  }
});

async function cli(args: string[], input?: Buffer): Promise<{ code: number | null; stdout: Buffer; stderr: Buffer }> {
  return await new Promise((resolveResult, reject) => {
    const child = spawn(process.execPath, [resolve("dist/cli.js"), ...args], { stdio: ["pipe", "pipe", "pipe"] });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk: Buffer) => stdout.push(Buffer.from(chunk)));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(Buffer.from(chunk)));
    child.on("error", reject);
    child.on("close", (code) => resolveResult({ code, stdout: Buffer.concat(stdout), stderr: Buffer.concat(stderr) }));
    child.stdin.end(input);
  });
}

describe("CLI secret output contract", () => {
  it("keeps multiline stdin and provider file contents out of all output", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-cli-test-"));
    temporaryDirectories.push(directory);
    const sourcePath = join(directory, "source.env");
    const destinationPath = join(directory, "destination.env");
    const brokenPath = join(directory, "broken.env");
    const configPath = join(directory, "keyper.json");
    await writeFile(sourcePath, "", { mode: 0o600 });
    await writeFile(destinationPath, "", { mode: 0o600 });
    const canary = Buffer.from(`${randomBytes(64).toString("base64url")}\n'\"\\second-line`);
    await writeFile(brokenPath, `DUPLICATE=\"${canary.toString()}\"\nDUPLICATE=again\n`, { mode: 0o600 });
    await writeFile(configPath, JSON.stringify({
      version: 1,
      aliases: {
        source: { provider: "local", path: sourcePath },
        destination: { provider: "local", path: destinationPath },
        broken: { provider: "local", path: brokenPath }
      },
      checks: {
        token: ["source:TOKEN", "destination:TOKEN"],
        broken: ["source:TOKEN", "broken:DUPLICATE"]
      }
    }));

    const put = await cli(["put", "TOKEN", "--stdin", "--to", "source", "--config", configPath], canary);
    const missingAudit = await cli(["audit", "token", "--config", configPath]);
    const failedAudit = await cli(["audit", "broken", "--config", configPath]);
    const copied = await cli(["copy", "TOKEN", "--from", "source", "--to", "destination", "--config", configPath]);
    const audit = await cli(["audit", "token", "--json", "--config", configPath]);
    const plannedRemove = await cli(["plan", "remove", "TOKEN", "--from", "destination", "--config", configPath]);
    const wrongRemove = await cli(["remove", "TOKEN", "--from", "destination", "--confirm-delete", "OTHER", "--config", configPath]);
    const removed = await cli(["remove", "TOKEN", "--from", "destination", "--confirm-delete", "TOKEN", "--config", configPath]);
    const invalidSync = await cli(["sync", "token", "--from", "source:OTHER", "--config", configPath]);
    const plannedSync = await cli(["plan", "sync", "token", "--from", "source:TOKEN", "--config", configPath]);
    const synced = await cli(["sync", "token", "--from", "source:TOKEN", "--config", configPath]);
    const copy = await cli(["copy-many", "TOKEN", "--from", "source", "--to", "destination:RENAMED", "--to", "broken", "--config", configPath]);
    const help = await cli(["compose", "--help"]);
    try {
      expect(put.code).toBe(0);
      expect(missingAudit.code).toBe(4);
      expect(failedAudit.code).toBe(5);
      expect(copied.code).toBe(0);
      expect(audit.code).toBe(0);
      expect(audit.stdout.toString("utf8")).toContain('"token": "equal"');
      expect(plannedRemove.code).toBe(0);
      expect(wrongRemove.code).toBe(5);
      expect(removed.code).toBe(0);
      expect(invalidSync.code).toBe(5);
      expect(plannedSync.code).toBe(0);
      expect(synced.code).toBe(0);
      expect(copy.code).toBe(5);
      expect(help.code).toBe(0);
      for (const output of [put.stdout, put.stderr, missingAudit.stdout, missingAudit.stderr, failedAudit.stdout, failedAudit.stderr, copied.stdout, copied.stderr, audit.stdout, audit.stderr, plannedRemove.stdout, plannedRemove.stderr, wrongRemove.stdout, wrongRemove.stderr, removed.stdout, removed.stderr, invalidSync.stdout, invalidSync.stderr, plannedSync.stdout, plannedSync.stderr, synced.stdout, synced.stderr, copy.stdout, copy.stderr, help.stdout, help.stderr]) {
        expect(output.includes(canary)).toBe(false);
      }
      const received = await new LocalProvider(sourcePath).read("TOKEN");
      try {
        expect(received.length === canary.length && timingSafeEqual(received, canary)).toBe(true);
      } finally {
        received.fill(0);
      }
      expect(await new LocalProvider(destinationPath).list()).toEqual([{ name: "TOKEN", portability: "portable" }]);
      expect((await readdir(directory)).filter((name) => name.includes(".keyper-"))).toEqual([]);
    } finally {
      put.stdout.fill(0); put.stderr.fill(0); missingAudit.stdout.fill(0); missingAudit.stderr.fill(0);
      failedAudit.stdout.fill(0); failedAudit.stderr.fill(0); copied.stdout.fill(0); copied.stderr.fill(0);
      audit.stdout.fill(0); audit.stderr.fill(0); plannedRemove.stdout.fill(0); plannedRemove.stderr.fill(0);
      wrongRemove.stdout.fill(0); wrongRemove.stderr.fill(0); removed.stdout.fill(0); removed.stderr.fill(0);
      invalidSync.stdout.fill(0); invalidSync.stderr.fill(0); plannedSync.stdout.fill(0); plannedSync.stderr.fill(0);
      synced.stdout.fill(0); synced.stderr.fill(0); copy.stdout.fill(0); copy.stderr.fill(0);
      help.stdout.fill(0); help.stderr.fill(0);
      canary.fill(0);
    }
  });
});
