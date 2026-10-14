import { randomBytes, timingSafeEqual } from "node:crypto";
import { mkdtemp, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { StructuredProvider } from "../src/providers/structured.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  while (temporaryDirectories.length > 0) {
    await rm(temporaryDirectories.pop()!, { recursive: true, force: true });
  }
});

describe("StructuredProvider", () => {
  it("updates only allowlisted fields with atomic mode-0600 writes", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-structured-test-"));
    temporaryDirectories.push(directory);
    const path = join(directory, "config.json");
    await writeFile(path, JSON.stringify({ auth: { token: "old", enabled: false }, retained: 7 }));
    const provider = new StructuredProvider(path, { TOKEN: "/auth/token", ENABLED: "/auth/enabled" });
    const value = Buffer.from(randomBytes(64).toString("base64url"));
    await provider.write("TOKEN", value, { overwrite: true, sensitive: false });
    await provider.write("ENABLED", Buffer.from("true"), { overwrite: true, sensitive: false, valueType: "boolean" });
    await provider.write("TOKEN", Buffer.from(JSON.stringify(["first", "second"])), { overwrite: true, sensitive: false, valueType: "json" });
    expect((await provider.read("TOKEN")).toString()).toBe('["first","second"]');
    await provider.write("TOKEN", value, { overwrite: true, sensitive: false });
    const received = await provider.read("TOKEN");
    expect(received.length === value.length && timingSafeEqual(received, value)).toBe(true);
    expect((await provider.read("ENABLED")).toString()).toBe("true");
    expect((await stat(path)).mode & 0o777).toBe(0o600);
    expect((await readdir(directory)).filter((name) => name.includes(".keyper-"))).toEqual([]);
    const raw = await readFile(path);
    try {
      expect(JSON.parse(raw.toString()).retained).toBe(7);
    } finally {
      raw.fill(0);
      received.fill(0);
      value.fill(0);
    }
  });

  it("returns only a sanitized error for malformed secret-bearing JSON", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-structured-test-"));
    temporaryDirectories.push(directory);
    const path = join(directory, "config.json");
    const canary = `${randomBytes(48).toString("base64")}\nsecond-line`;
    await writeFile(path, `{\"token\":\"${canary}`);
    let message = "";
    try {
      await new StructuredProvider(path, { TOKEN: "/token" }).list();
    } catch (error: unknown) {
      message = error instanceof Error ? error.message : "";
    }
    expect(message).toBe("The structured configuration file or allowlisted field is invalid.");
    expect(message.includes(canary)).toBe(false);
    expect((await readdir(directory)).filter((name) => name.includes(".keyper-"))).toEqual([]);
  });
});
