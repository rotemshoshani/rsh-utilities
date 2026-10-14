import { randomBytes } from "node:crypto";
import { mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { KeyperError } from "../src/errors.js";
import { VercelProvider } from "../src/providers/vercel.js";
import type { SafeProcessOptions } from "../src/safe-process.js";

const directories: string[] = [];
const originalToken = process.env.VERCEL_TOKEN;
afterEach(async () => {
  if (originalToken === undefined) delete process.env.VERCEL_TOKEN;
  else process.env.VERCEL_TOKEN = originalToken;
  while (directories.length) await rm(directories.pop()!, { recursive: true, force: true });
});

function fixture(failure?: string) {
  let payload: Buffer | undefined;
  const response = Buffer.from(randomBytes(24).toString("hex"));
  let apiArgs: string[] = [];
  let contextDirectory = "";
  let validRequest = false;
  const runner = async (_command: string, args: readonly string[], options?: SafeProcessOptions) => {
    const directory = args[args.indexOf("--cwd") + 1];
    if (args[0] === "link") {
      contextDirectory = directory;
      directories.push(directory);
      await mkdir(join(directory, ".vercel"));
      await writeFile(join(directory, ".vercel", "project.json"), JSON.stringify({ orgId: "team-1", projectId: "project-1" }));
      return Buffer.alloc(0);
    }
    if (args[0] === "env" && args[1] === "list") {
      return Buffer.from(JSON.stringify([{ key: "API_KEY", id: "env-1", type: "sensitive" }]));
    }
    if (args[0] === "api") {
      apiArgs = [...args];
      payload = options?.input;
      const body = JSON.parse(payload?.toString("utf8") ?? "{}");
      validRequest = body.key === "API_KEY" && body.type === "sensitive" && body.target?.[0] === "production" && typeof body.value === "string";
      if (failure) {
        const stderr = Buffer.from(failure);
        try { throw new KeyperError(options?.classifyFailure?.(stderr, Buffer.alloc(0)) ?? "PROVIDER_FAILED"); }
        finally { stderr.fill(0); }
      }
      return response;
    }
    throw new Error("Unexpected provider command");
  };
  const provider = new VercelProvider({ provider: "vercel", project: "app", environment: "production" }, runner);
  return { provider, response, args: () => apiArgs, payload: () => payload, directory: () => contextDirectory, validRequest: () => validRequest };
}

describe("Vercel CLI authenticated writes", () => {
  it("uses native CLI authentication and sends secret values only through stdin", async () => {
    delete process.env.VERCEL_TOKEN;
    const f = fixture();
    const value = Buffer.from(randomBytes(32).toString("hex"));
    const original = Buffer.from(value);
    const result = await f.provider.write("API_KEY", value, { overwrite: true, sensitive: true });
    expect(result).toEqual({ verifiable: false });
    expect(f.validRequest()).toBe(true);
    expect(f.args()).toContain("--silent");
    expect(f.args()).toContain("--input");
    expect(f.args()).toContain("-");
    expect(f.args()[1]).toBe("/v10/projects/project-1/env?upsert=true&teamId=team-1");
    expect(f.args().some(arg => arg.includes(original.toString("utf8")))).toBe(false);
    expect(f.args()).not.toContain("--token");
    expect(f.payload()?.every(byte => byte === 0)).toBe(true);
    expect(f.response.every(byte => byte === 0)).toBe(true);
    expect(value.equals(original)).toBe(true);
    value.fill(0); original.fill(0);
  });

  it("keeps an explicit token override in an owner-only temporary CLI file", async () => {
    const token = randomBytes(32).toString("hex");
    process.env.VERCEL_TOKEN = token;
    const f = fixture();
    await f.provider.write("API_KEY", Buffer.from("fixture-value"), { overwrite: true, sensitive: true });
    expect(f.args()).toContain("--global-config");
    expect(f.args().some(arg => arg.includes(token))).toBe(false);
    const path = join(f.directory(), "auth.json");
    expect((await stat(path)).mode & 0o777).toBe(0o600);
    expect((await stat(f.directory())).mode & 0o777).toBe(0o700);
    const bytes = await readFile(path);
    try { expect(JSON.parse(bytes.toString("utf8")).token === token).toBe(true); }
    finally { bytes.fill(0); }
  });

  it.each([
    ["HTTP 401", "PROVIDER_AUTH"],
    ["HTTP 403 Forbidden", "PROVIDER_AUTH"],
    ["HTTP 409", "DESTINATION_EXISTS"],
    ["HTTP 422", "CONFIG_INVALID"],
    ["transport failure", "PROVIDER_FAILED"],
  ])("sanitizes provider errors and clears the request body on %s", async (message, code) => {
    delete process.env.VERCEL_TOKEN;
    const canary = randomBytes(32).toString("hex");
    const f = fixture(`${message}: ${canary}`);
    let caught: unknown;
    try { await f.provider.write("API_KEY", Buffer.from("fixture-value"), { overwrite: true, sensitive: true }); }
    catch (error) { caught = error; }
    expect(caught).toMatchObject({ code });
    expect(String(caught).includes(canary)).toBe(false);
    expect(f.payload()?.every(byte => byte === 0)).toBe(true);
  });
});
