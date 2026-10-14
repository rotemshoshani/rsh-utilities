import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { addAlias, addIssuer, loadConfig } from "../src/config.js";

const temporaryDirectories: string[] = [];
afterEach(async () => {
  while (temporaryDirectories.length > 0) await rm(temporaryDirectories.pop()!, { recursive: true, force: true });
});

describe("config mutation", () => {
  it("adds aliases and issuers without accepting credential values", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-config-test-"));
    temporaryDirectories.push(directory);
    const path = join(directory, "keyper.json");
    const originalAlias = { path: "secrets.env", provider: "local" as const };
    await writeFile(path, JSON.stringify({
      version: 1,
      aliases: { local: originalAlias },
      checks: { localPair: ["local:FIRST", "local:SECOND"] }
    }), { mode: 0o600 });
    await addAlias(path, "remote", {
      provider: "ssh-structured",
      host: "safe-host",
      path: "/srv/app/config.json",
      fields: { TOKEN: "/auth/token" }
    });
    await addIssuer(path, "cloudflare", {
      provider: "cloudflare-access-service-token",
      accountId: { alias: "local", name: "ACCOUNT_ID" },
      apiToken: { alias: "local", name: "API_TOKEN" },
      tokenName: "service-token"
    });
    const loaded = await loadConfig(path);
    const raw = JSON.parse(await readFile(path, "utf8"));
    expect(raw.aliases.local).toEqual(originalAlias);
    expect(loaded.config.aliases.remote?.provider).toBe("ssh-structured");
    expect(loaded.config.issuers?.cloudflare?.provider).toBe("cloudflare-access-service-token");
    expect(loaded.config.checks?.localPair).toEqual([
      { alias: "local", name: "FIRST" },
      { alias: "local", name: "SECOND" }
    ]);
  });

  it("rejects invalid audit checks", async () => {
    const directory = await mkdtemp(join(tmpdir(), "keyper-config-test-"));
    temporaryDirectories.push(directory);
    const invalidChecks = [
      { short: ["local:FIRST"] },
      { duplicate: ["local:FIRST", "local:FIRST"] },
      { unknown: ["local:FIRST", "missing:SECOND"] },
      { "invalid name": ["local:FIRST", "local:SECOND"] }
    ];
    for (const [index, checks] of invalidChecks.entries()) {
      const path = join(directory, `${index}.json`);
      await writeFile(path, JSON.stringify({ version: 1, aliases: { local: { provider: "local", path: "secrets.env" } }, checks }));
      await expect(loadConfig(path)).rejects.toMatchObject({ code: "CONFIG_INVALID" });
    }
  });
});
