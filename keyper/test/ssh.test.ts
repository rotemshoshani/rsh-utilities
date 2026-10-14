import { randomBytes, timingSafeEqual } from "node:crypto";
import { mkdtemp, readdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { parse } from "dotenv";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { runSafeProcess } from "../src/safe-process.js";
import { REMOTE_SCRIPT } from "../src/providers/ssh.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  while (temporaryDirectories.length > 0) {
    await rm(temporaryDirectories.pop()!, { recursive: true, force: true });
  }
});

function metadata(path: string, op: string, name?: string, overwrite = false): string {
  return Buffer.from(JSON.stringify({ path, format: "dotenv", op, name, overwrite })).toString("base64url");
}

describe("SSH remote file program", () => {
  it("round-trips multiline stdin with atomic mode-0600 writes", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-ssh-test-"));
    temporaryDirectories.push(directory);
    const path = join(directory, "remote.env");
    const value = Buffer.from(`${randomBytes(64).toString("base64url")}\n'\"\\second-line`);
    const written = await runSafeProcess("python3", ["-c", REMOTE_SCRIPT, metadata(path, "write", "TOKEN")], { input: value });
    written.fill(0);
    const received = await runSafeProcess("python3", ["-c", REMOTE_SCRIPT, metadata(path, "read", "TOKEN")]);
    try {
      expect(received.length === value.length && timingSafeEqual(received, value)).toBe(true);
      expect(parse(await readFile(path)).TOKEN === value.toString()).toBe(true);
      expect((await stat(path)).mode & 0o777).toBe(0o600);
      expect((await readdir(directory)).filter((name) => name.startsWith(".keyper-"))).toEqual([]);
    } finally {
      received.fill(0);
      value.fill(0);
    }
  });

  it("writes JSON that the application's dotenv parser can consume", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-ssh-test-"));
    temporaryDirectories.push(directory);
    const path = join(directory, "remote.env");
    const json = JSON.stringify({ installed: { client_id: "dummy", client_secret: "dummy\\n#quoted\"value" } }, null, 2);
    const written = await runSafeProcess("python3", ["-c", REMOTE_SCRIPT, metadata(path, "write", "JSON")], { input: Buffer.from(json) });
    written.fill(0);
    expect(parse(await readFile(path)).JSON === json).toBe(true);
    const received = await runSafeProcess("python3", ["-c", REMOTE_SCRIPT, metadata(path, "read", "JSON")]);
    expect(received.toString() === json).toBe(true);
    received.fill(0);
  });

  it("preserves systemd escaping independently of dotenv", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-ssh-test-"));
    temporaryDirectories.push(directory);
    const path = join(directory, "remote.env");
    const meta = (op: string) => Buffer.from(JSON.stringify({ path, format: "systemd", op, name: "TOKEN" })).toString("base64url");
    const value = Buffer.from('dummy\\n"quoted"\\path');
    (await runSafeProcess("python3", ["-c", REMOTE_SCRIPT, meta("write")], { input: value })).fill(0);
    const received = await runSafeProcess("python3", ["-c", REMOTE_SCRIPT, meta("read")]);
    expect(received.equals(value)).toBe(true);
    received.fill(0);
  });

  it("suppresses multiline file contents on remote parse failure", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-ssh-test-"));
    temporaryDirectories.push(directory);
    const path = join(directory, "remote.env");
    const canary = `${randomBytes(48).toString("base64url")}\nsecond-line`;
    await writeFile(path, `DUPLICATE=\"${canary}\"\nDUPLICATE=again\n`, { mode: 0o600 });
    let message = "";
    try {
      await runSafeProcess("python3", ["-c", REMOTE_SCRIPT, metadata(path, "list")]);
    } catch (error: unknown) {
      message = error instanceof Error ? error.message : "";
    }
    expect(message).toBe("The provider operation failed. Provider output was suppressed.");
    expect(message.includes(canary)).toBe(false);
    expect((await readdir(directory)).filter((name) => name.startsWith(".keyper-"))).toEqual([]);
  });
});
