import { randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { secretForms, scrub } from "../src/run.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  while (temporaryDirectories.length > 0) {
    await rm(temporaryDirectories.pop()!, { recursive: true, force: true });
  }
});

interface CliResult { code: number | null; stdout: string; stderr: string }

async function cli(args: string[], options: { cwd?: string; configHome: string }): Promise<CliResult> {
  return await new Promise((resolveResult, reject) => {
    const child = spawn(process.execPath, [resolve("dist/cli.js"), ...args], {
      cwd: options.cwd ?? process.cwd(),
      env: { ...process.env, XDG_CONFIG_HOME: options.configHome },
      stdio: ["ignore", "pipe", "pipe"]
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on("data", (chunk: Buffer) => stdout.push(Buffer.from(chunk)));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(Buffer.from(chunk)));
    child.on("error", reject);
    child.on("close", (code) => resolveResult({
      code,
      stdout: Buffer.concat(stdout).toString("utf8"),
      stderr: Buffer.concat(stderr).toString("utf8")
    }));
  });
}

// The fixture tool prints the injected value in a selected form; it never receives it in argv.
const TOOL_SCRIPT = String.raw`
const fs = require("node:fs");
const value = process.env.API_TOKEN ?? "";
const mode = process.argv[2];
const bytes = Buffer.from(value, "utf8");
if (mode === "plain") console.log("token=" + value);
if (mode === "forms") {
  console.log(JSON.stringify({
    base64: bytes.toString("base64"), base64url: bytes.toString("base64url"),
    url: encodeURIComponent(value), form: encodeURIComponent(value).replace(/%20/g, "+"),
    hex: bytes.toString("hex"), HEX: bytes.toString("hex").toUpperCase(),
    basic: "Basic " + Buffer.from("user:" + value).toString("base64"),
    trimmed: value.trim(), json: JSON.stringify(value)
  }));
}
if (mode === "multiline") process.stdout.write("pem:\n" + (process.env.MULTI ?? "") + "\nend\n");
if (mode === "stderr") { process.stderr.write("failed with " + value + "\n"); process.exit(3); }
if (mode === "argv") {
  const own = fs.readFileSync("/proc/self/cmdline");
  const parent = fs.readFileSync("/proc/" + process.ppid + "/cmdline");
  const leaked = own.includes(bytes) || parent.includes(bytes);
  console.log((value ? "env-present " : "env-missing ") + (leaked ? "argv-leak" : "argv-clean") + " args=" + JSON.stringify(process.argv.slice(2)));
}
if (mode === "binary") process.stdout.write(Buffer.from([0x00, 0xff, 0xfe, 0x01]));
if (mode === "big") process.stdout.write("x".repeat(4096));
if (mode === "sleep") setInterval(() => {}, 1000);
`;

async function fixture(): Promise<{ directory: string; configHome: string; config: string; canary: string; multi: string }> {
  const directory = await mkdtemp(join(tmpdir(), "keyper-run-test-"));
  temporaryDirectories.push(directory);
  const configHome = join(directory, "xdg");
  await mkdir(configHome);
  // A value with characters that change under every encoding, a non-ASCII letter, and padding.
  const canary = `${randomBytes(24).toString("base64url")}+/= &?é${randomBytes(5).toString("hex")}`;
  const multi = `-----BEGIN KEY-----\n${randomBytes(30).toString("base64")}\n${randomBytes(30).toString("base64")}\n-----END KEY-----`;
  await writeFile(join(directory, ".env.local"), `API_TOKEN="${canary}"\nMULTI="${multi}"\n`, { mode: 0o600 });
  await writeFile(join(directory, "tool.cjs"), TOOL_SCRIPT);
  const config = join(directory, ".keyper.json");
  await writeFile(config, JSON.stringify({
    version: 1,
    aliases: { local: { provider: "local", path: ".env.local" } },
    tools: {
      fixture: { command: [process.execPath, "tool.cjs"], env: { API_TOKEN: "local:API_TOKEN", MULTI: "local:MULTI" } },
      small: { command: [process.execPath, "tool.cjs"], env: { API_TOKEN: "local:API_TOKEN" }, maxOutputBytes: 1024 },
      slow: { command: [process.execPath, "tool.cjs"], env: { API_TOKEN: "local:API_TOKEN" }, timeoutSeconds: 1 },
      dumper: { command: ["printenv"], env: { API_TOKEN: "local:API_TOKEN" } },
      envwrap: { command: ["/usr/bin/env"], env: { API_TOKEN: "local:API_TOKEN" } },
      reader: { command: ["cat", ".env.local"], env: { API_TOKEN: "local:API_TOKEN" } },
      shell: { command: ["bash", "-c", "true"], env: { API_TOKEN: "local:API_TOKEN" } },
      inline: { command: ["python3", "-c", "pass"], env: { API_TOKEN: "local:API_TOKEN" } },
      bare: { command: ["node"], env: { API_TOKEN: "local:API_TOKEN" } },
      sneaky: { command: [process.execPath, "tool.cjs", "--env-file=.env.local"], env: { API_TOKEN: "local:API_TOKEN" } }
    }
  }));
  return { directory, configHome, config, canary, multi };
}

function encodings(value: string): string[] {
  const bytes = Buffer.from(value, "utf8");
  return [
    value, bytes.toString("base64"), bytes.toString("base64").replace(/=+$/, ""), bytes.toString("base64url"),
    encodeURIComponent(value), bytes.toString("hex"), bytes.toString("hex").toUpperCase(),
    JSON.stringify(value).slice(1, -1)
  ];
}

function expectNoValue(results: CliResult[], values: string[]): void {
  for (const result of results) {
    for (const output of [result.stdout, result.stderr]) {
      for (const value of values) for (const form of encodings(value)) expect(output.includes(form)).toBe(false);
    }
  }
}

describe("secret scrubbing", () => {
  it("redacts overlapping and embedded forms", () => {
    const secret = Buffer.from("abcabcabcXYZ123456");
    const forms = secretForms(secret);
    expect(scrub("pre abcabcabcXYZ123456abcabcabcXYZ123456 post", forms)).toEqual({ text: "pre [redacted] post", redactions: 1 });
    const basic = Buffer.from(`u:${secret.toString()}`).toString("base64");
    expect(scrub(basic, forms).text).not.toContain(Buffer.from(secret).toString("base64").slice(4, 16));
    expect(scrub(basic, forms).redactions).toBe(1);
  });

  it("redacts each line of a multi-line value even when reflowed", () => {
    const secret = Buffer.from("first-line-1234\r\nsecond-line-5678");
    expect(scrub("a first-line-1234 b second-line-5678 c", secretForms(secret)).text).toBe("a [redacted] b [redacted] c");
  });
});

describe("keyper run", () => {
  it("redacts plain, base64, URL-encoded, hex, JSON, and Basic-auth forms from stdout", async () => {
    const { configHome, config, canary, multi } = await fixture();
    const result = await cli(["run", "fixture", "--config", config, "--", "forms"], { configHome });
    expect(result.code).toBe(0);
    const parsed = JSON.parse(result.stdout) as Record<string, string>;
    for (const field of ["base64", "base64url", "url", "form", "hex", "HEX", "trimmed"]) expect(parsed[field]).toBe("[redacted]");
    expect(parsed.json).toBe('"[redacted]"');
    expect(parsed.basic).toMatch(/^Basic dXNlcj.\[redacted\].{0,4}$/);
    expect(result.stderr).toBe("run\tfixture\tinjected=API_TOKEN,MULTI\tredacted=9\texit=0\n");
    expectNoValue([result], [canary, multi]);
  });

  it("redacts plain and multi-line values", async () => {
    const { configHome, config, canary, multi } = await fixture();
    const plain = await cli(["run", "fixture", "--config", config, "--", "plain"], { configHome });
    const lines = await cli(["run", "fixture", "--config", config, "--", "multiline"], { configHome });
    expect(plain.stdout).toBe("token=[redacted]\n");
    expect(lines.stdout).toBe("pem:\n[redacted]\nend\n");
    expectNoValue([plain, lines], [canary, multi, ...multi.split("\n").filter((line) => line.length >= 8)]);
  });

  it("redacts stderr and returns the tool's non-zero exit code", async () => {
    const { configHome, config, canary } = await fixture();
    const result = await cli(["run", "fixture", "--config", config, "--", "stderr"], { configHome });
    expect(result.code).toBe(3);
    expect(result.stderr).toContain("failed with [redacted]\n");
    expect(result.stderr).toContain("exit=3");
    expectNoValue([result], [canary]);
  });

  it.skipIf(!existsSync("/proc/self/cmdline"))("injects through the environment, never argv, and passes arguments verbatim", async () => {
    const { configHome, config, canary } = await fixture();
    const result = await cli(["run", "fixture", "--config", config, "--", "argv", "--start", "2026-10-27", "a b"], { configHome });
    expect(result.code).toBe(0);
    expect(result.stdout).toBe('env-present argv-clean args=["argv","--start","2026-10-27","a b"]\n');
    expectNoValue([result], [canary]);
  });

  it("refuses undeclared tools, value dumpers, shells, inline code, and env-file arguments", async () => {
    const { configHome, config, canary, directory } = await fixture();
    const results: CliResult[] = [];
    const missing = await cli(["run", "nope", "--config", config, "--", "plain"], { configHome });
    results.push(missing);
    expect(missing.code).toBe(5);
    expect(missing.stderr).toContain("error TOOL_NOT_FOUND");
    for (const tool of ["dumper", "envwrap", "reader", "shell", "inline", "bare", "sneaky"]) {
      const refused = await cli(["run", tool, "--config", config], { configHome });
      results.push(refused);
      expect(refused.code).toBe(5);
      expect(refused.stderr).toContain("error TOOL_UNSAFE");
    }
    for (const argument of [".env.local", join(directory, ".env.local"), "--file=/proc/self/environ"]) {
      const refused = await cli(["run", "fixture", "--config", config, "--", "plain", argument], { configHome });
      results.push(refused);
      expect(refused.stderr).toContain("error TOOL_UNSAFE");
    }
    expectNoValue(results, [canary]);
  });

  it("fails closed without output for binary, oversized, and timed-out tools", async () => {
    const { configHome, config, canary } = await fixture();
    const binary = await cli(["run", "fixture", "--config", config, "--", "binary"], { configHome });
    const big = await cli(["run", "small", "--config", config, "--", "big"], { configHome });
    const slow = await cli(["run", "slow", "--config", config, "--", "sleep"], { configHome });
    expect([binary.code, big.code, slow.code]).toEqual([5, 5, 5]);
    expect(binary.stdout + big.stdout + slow.stdout).toBe("");
    expect(binary.stderr).toContain("error OUTPUT_UNSAFE");
    expect(big.stderr).toContain("error OUTPUT_TOO_LARGE");
    expect(slow.stderr).toContain("error TOOL_TIMEOUT");
    expectNoValue([binary, big, slow], [canary]);
  });

  it("runs the garmin wrapper through the same mechanism", async () => {
    const { configHome, config, canary, directory } = await fixture();
    await writeFile(config, JSON.stringify({
      version: 1,
      aliases: { local: { provider: "local", path: ".env.local" } },
      tools: { garmin: { command: [process.execPath, "tool.cjs"], env: { API_TOKEN: "local:API_TOKEN" } } }
    }));
    const result = await cli(["garmin", "plain"], { cwd: directory, configHome });
    expect(result.code).toBe(0);
    expect(result.stdout).toBe("token=[redacted]\n");
    expect(result.stderr).toBe("run\tgarmin\tinjected=API_TOKEN\tredacted=1\texit=0\n");
    expectNoValue([result], [canary]);
  });
});

describe("config discovery", () => {
  it("finds .keyper.json in a parent directory, then the user config, and names the paths it tried", async () => {
    const { directory, configHome } = await fixture();
    const nested = join(directory, "a", "b");
    await mkdir(nested, { recursive: true });
    const found = await cli(["list", "--source", "local"], { cwd: nested, configHome });
    expect(found.code).toBe(0);
    expect(found.stdout).toBe("local\tAPI_TOKEN\tportable\nlocal\tMULTI\tportable\n");

    const empty = await mkdtemp(join(tmpdir(), "keyper-empty-test-"));
    temporaryDirectories.push(empty);
    const missing = await cli(["list"], { cwd: empty, configHome });
    expect(missing.code).toBe(5);
    expect(missing.stderr).toContain(`Tried .keyper.json in ${empty} and each parent directory, then ${join(configHome, "keyper", "config.json")}.`);

    await mkdir(join(configHome, "keyper"));
    await writeFile(join(configHome, "keyper", "config.json"), JSON.stringify({
      version: 1, aliases: { global: { provider: "local", path: join(directory, ".env.local") } }
    }));
    const global = await cli(["list"], { cwd: empty, configHome });
    expect(global.stdout).toContain("global\tAPI_TOKEN\tportable");

    await writeFile(join(configHome, "keyper", "config.json"), "{");
    const invalid = await cli(["list"], { cwd: empty, configHome });
    expect(invalid.stderr).toContain(`Tried ${join(configHome, "keyper", "config.json")}.`);
  });
});

describe("keyper doctor file status", () => {
  async function git(cwd: string, args: string[]): Promise<void> {
    await new Promise<void>((resolveGit, reject) => {
      const child = spawn("git", args, { cwd, stdio: "ignore" });
      child.on("error", reject);
      child.on("close", (code) => code === 0 ? resolveGit() : reject(new Error(`git ${args[0]} failed`)));
    });
  }

  it("warns when a local env file is untracked and not gitignored, by name and status only", async () => {
    const { directory, configHome, config, canary } = await fixture();
    await writeFile(config, JSON.stringify({
      version: 1,
      aliases: { local: { provider: "local", path: ".env.local" } },
      tools: {
        fixture: { command: [process.execPath, "tool.cjs"], env: { API_TOKEN: "local:API_TOKEN" } },
        missing: { command: [process.execPath, "tool.cjs"], env: { API_TOKEN: "local:ABSENT" } },
        dumper: { command: ["printenv"], env: { API_TOKEN: "local:API_TOKEN" } }
      }
    }));
    const outside = await cli(["doctor", "--json", "--config", config], { configHome });
    expect(JSON.parse(outside.stdout)).toEqual({
      aliases: { local: "ok" }, files: { local: "not-in-repo" },
      tools: { dumper: "unsafe", fixture: "ok", missing: "missing" }
    });

    await git(directory, ["init", "-q"]);
    const exposed = await cli(["doctor", "--config", config], { configHome });
    expect(exposed.stdout).toContain("file\tlocal\tuntracked-not-ignored\n");
    expect(exposed.stderr).toContain("warning FILE_NOT_IGNORED: The file for alias local is untracked and not gitignored");

    await writeFile(join(directory, ".gitignore"), ".env.local\n");
    const ignored = await cli(["doctor", "--json", "--config", config], { configHome });
    expect(JSON.parse(ignored.stdout).files).toEqual({ local: "ignored" });
    expect(ignored.stderr).toBe("");
    expectNoValue([outside, exposed, ignored], [canary]);
  });

  it("exits 4 for a file warning when everything else is healthy", async () => {
    const { directory, configHome, config } = await fixture();
    await writeFile(config, JSON.stringify({ version: 1, aliases: { local: { provider: "local", path: ".env.local" } } }));
    await git(directory, ["init", "-q"]);
    expect((await cli(["doctor", "--config", config], { configHome })).code).toBe(4);
  });
});
