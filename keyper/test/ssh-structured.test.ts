import { randomBytes, timingSafeEqual } from "node:crypto";
import { mkdtemp, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { runSafeProcess } from "../src/safe-process.js";
import { REMOTE_STRUCTURED_SCRIPT } from "../src/providers/ssh-structured.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  while (temporaryDirectories.length > 0) await rm(temporaryDirectories.pop()!, { recursive: true, force: true });
});

function metadata(path: string, op: string, name?: string, valueType = "string", overwrite = false): string {
  return Buffer.from(JSON.stringify({
    path,
    op,
    name,
    valueType,
    overwrite,
    fields: { TOKEN: "/auth/token", KEYS: "/auth/keys" }
  })).toString("base64url");
}

describe("SSH structured remote program", () => {
  it("updates allowlisted JSON fields atomically without disturbing siblings", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-ssh-structured-test-"));
    temporaryDirectories.push(directory);
    const path = join(directory, "config.json");
    await writeFile(path, JSON.stringify({ auth: { token: "old" }, retained: 7 }), { mode: 0o600 });
    const token = Buffer.from(randomBytes(48).toString("base64url"));
    const keys = Buffer.from(JSON.stringify(["first", "second"]));
    (await runSafeProcess("python3", ["-c", REMOTE_STRUCTURED_SCRIPT, metadata(path, "write", "TOKEN", "string", true)], { input: token })).fill(0);
    (await runSafeProcess("python3", ["-c", REMOTE_STRUCTURED_SCRIPT, metadata(path, "write", "KEYS", "json", false)], { input: keys })).fill(0);
    const received = await runSafeProcess("python3", ["-c", REMOTE_STRUCTURED_SCRIPT, metadata(path, "read", "TOKEN")]);
    const raw = await readFile(path);
    try {
      expect(received.length === token.length && timingSafeEqual(received, token)).toBe(true);
      const parsed = JSON.parse(raw.toString()) as { auth: { keys: string[] }; retained: number };
      expect(parsed.auth.keys).toEqual(["first", "second"]);
      expect(parsed.retained).toBe(7);
      expect((await stat(path)).mode & 0o777).toBe(0o600);
      expect((await readdir(directory)).filter((name) => name.startsWith(".keyper-"))).toEqual([]);
    } finally {
      token.fill(0); keys.fill(0); received.fill(0); raw.fill(0);
    }
  });
});
