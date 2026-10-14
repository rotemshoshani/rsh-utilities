import { chmod, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { KeyperError } from "../errors.js";
import { runSafeProcess } from "../safe-process.js";
import type { InventoryEntry, KeyProvider, VercelAlias, WriteOptions } from "../types.js";

const READ_HELPER = [
  "const n=process.argv[1];",
  "if(!Object.prototype.hasOwnProperty.call(process.env,n))process.exit(42);",
  "process.stdout.write(process.env[n]??'');"
].join("");

const contextDirectories = new Set<string>();
let cleanupRegistered = false;

interface VercelContext {
  directory: string;
  orgId: string;
  projectId: string;
  authArgs: string[];
}

interface VercelInventoryEntry extends InventoryEntry {
  id?: string;
}

function registerContextCleanup(directory: string): void {
  contextDirectories.add(directory);
  if (cleanupRegistered) return;
  cleanupRegistered = true;
  process.once("exit", () => {
    for (const path of contextDirectories) {
      try {
        rmSync(path, { recursive: true, force: true });
      } catch {
        // Best-effort cleanup; directories and credential files are owner-only.
      }
    }
  });
}

function extractEntries(value: unknown): VercelInventoryEntry[] {
  const candidates = Array.isArray(value)
    ? value
    : value && typeof value === "object"
      ? Object.values(value as Record<string, unknown>).find(Array.isArray) ?? []
      : [];
  const entries: VercelInventoryEntry[] = [];
  for (const candidate of candidates as unknown[]) {
    if (!candidate || typeof candidate !== "object") continue;
    const record = candidate as Record<string, unknown>;
    const name = typeof record.key === "string"
      ? record.key
      : typeof record.name === "string"
        ? record.name
        : undefined;
    if (!name) continue;
    const locked = record.sensitive === true || record.type === "sensitive";
    const id = typeof record.id === "string" ? record.id : undefined;
    entries.push({ name, portability: locked ? "locked" : "portable", ...(id ? { id } : {}) });
  }
  return entries.sort((left, right) => left.name.localeCompare(right.name));
}

export class VercelProvider implements KeyProvider {
  readonly kind = "vercel" as const;
  private inventoryCache: VercelInventoryEntry[] | undefined;
  private contextPromise: Promise<VercelContext> | undefined;

  constructor(
    private readonly config: VercelAlias,
    private readonly processRunner: typeof runSafeProcess = runSafeProcess
  ) {}

  private async context(): Promise<VercelContext> {
    if (!this.contextPromise) {
      this.contextPromise = (async () => {
        const directory = await mkdtemp(join(tmpdir(), "keyper-vercel-"));
        await chmod(directory, 0o700);
        try {
          // Let the CLI manage its own login and OAuth refresh. An explicit
          // token override stays in a private file, never a command argument.
          const token = process.env.VERCEL_TOKEN;
          const authArgs = token ? ["--global-config", directory] : [];
          if (token) {
            const bytes = Buffer.from(JSON.stringify({ token }), "utf8");
            try {
              await writeFile(join(directory, "auth.json"), bytes, { mode: 0o600, flag: "wx" });
            } finally {
              bytes.fill(0);
            }
          }
          const output = await this.processRunner("vercel", [
            "link",
            "--yes",
            "--project",
            this.config.project,
            "--cwd",
            directory,
            "--non-interactive",
            "--no-color",
            ...authArgs,
            ...(this.config.scope ? ["--scope", this.config.scope] : [])
          ]);
          output.fill(0);
          const metadataBytes = await readFile(join(directory, ".vercel", "project.json"));
          try {
            const metadata = JSON.parse(metadataBytes.toString("utf8")) as Record<string, unknown>;
            if (typeof metadata.orgId !== "string" || typeof metadata.projectId !== "string") {
              throw new KeyperError("PROVIDER_FAILED");
            }
            registerContextCleanup(directory);
            return { directory, orgId: metadata.orgId, projectId: metadata.projectId, authArgs };
          } finally {
            metadataBytes.fill(0);
          }
        } catch {
          rmSync(directory, { recursive: true, force: true });
          throw new KeyperError("PROVIDER_FAILED");
        }
      })();
    }
    return await this.contextPromise;
  }

  private async contextArgs(): Promise<string[]> {
    const context = await this.context();
    return ["--cwd", context.directory, "--non-interactive", "--no-color", ...context.authArgs];
  }

  private async apiRequest(
    apiVersion: string,
    suffix: string,
    init: { method: "POST" | "PATCH" | "DELETE"; body?: Buffer }
  ): Promise<void> {
    const context = await this.context();
    const separator = suffix.includes("?") ? "&" : "?";
    const endpoint = `/${apiVersion}/projects/${encodeURIComponent(context.projectId)}/env${suffix}${separator}teamId=${encodeURIComponent(context.orgId)}`;
    const output = await this.processRunner("vercel", [
      "api", endpoint, "--method", init.method, "--silent",
      ...(init.body ? ["--input", "-"] : []),
      ...await this.contextArgs()
    ], {
      ...(init.body ? { input: init.body } : {}),
      classifyFailure: (stderr, stdout) => {
        // Inspect captured errors internally; never forward provider messages.
        const bytes = Buffer.concat([stderr, stdout]);
        try {
          const error = bytes.toString("utf8");
          if (/\b(?:401|403)\b|unauthori[sz]ed|forbidden|authentication|not logged in|invalid token/i.test(error)) return "PROVIDER_AUTH";
          if (/\b409\b|already exists/i.test(error)) return "DESTINATION_EXISTS";
          if (/\b(?:400|422)\b|bad request|unprocessable/i.test(error)) return "CONFIG_INVALID";
          return "PROVIDER_FAILED";
        } finally {
          bytes.fill(0);
        }
      }
    });
    output.fill(0);
  }

  private targetArgs(): string[] {
    return [this.config.environment, ...(this.config.gitBranch ? [this.config.gitBranch] : [])];
  }

  async list(): Promise<InventoryEntry[]> {
    if (this.inventoryCache) return this.inventoryCache.map(({ name, portability }) => ({ name, portability }));
    const context = await this.contextArgs();
    const output = await this.processRunner("vercel", [
      "env",
      "list",
      ...this.targetArgs(),
      "--format",
      "json",
      ...context
    ]);
    try {
      this.inventoryCache = extractEntries(JSON.parse(output.toString("utf8")));
      return this.inventoryCache.map(({ name, portability }) => ({ name, portability }));
    } catch {
      throw new KeyperError("PROVIDER_FAILED");
    } finally {
      output.fill(0);
    }
  }

  async read(name: string): Promise<Buffer> {
    await this.list();
    const entry = this.inventoryCache?.find((candidate) => candidate.name === name);
    if (!entry) throw new KeyperError("SOURCE_MISSING");
    if (entry.portability === "locked") throw new KeyperError("VALUE_LOCKED");
    const context = await this.contextArgs();
    return await this.processRunner("vercel", [
      "env",
      "run",
      "-e",
      this.config.environment,
      ...(this.config.gitBranch ? ["--git-branch", this.config.gitBranch] : []),
      ...context,
      "--",
      process.execPath,
      "-e",
      READ_HELPER,
      name
    ]);
  }

  async write(name: string, value: Buffer, options: WriteOptions): Promise<{ verifiable: boolean }> {
    if (options.sensitive && !["preview", "production"].includes(this.config.environment)) {
      throw new KeyperError("CONFIG_INVALID");
    }
    await this.list();
    const entry = this.inventoryCache?.find((candidate) => candidate.name === name);
    if (entry && !options.overwrite) throw new KeyperError("DESTINATION_EXISTS");
    if (entry?.portability === "locked" && !options.sensitive) {
      throw new KeyperError("DESTINATION_LOCKED");
    }
    const requestBody = Buffer.from(JSON.stringify({
      key: name,
      value: value.toString("utf8"),
      type: options.sensitive ? "sensitive" : "encrypted",
      target: [this.config.environment],
      ...(this.config.gitBranch ? { gitBranch: this.config.gitBranch } : {})
    }), "utf8");
    try {
      await this.apiRequest(
        "v10",
        entry ? "?upsert=true" : "",
        { method: "POST", body: requestBody }
      );
    } finally {
      requestBody.fill(0);
    }
    if (!entry) {
      this.inventoryCache = undefined;
      const refreshed = await this.list();
      if (!refreshed.some((candidate) => candidate.name === name)) {
        throw new KeyperError("PROVIDER_FAILED");
      }
    } else {
      const remaining = (this.inventoryCache ?? []).filter((candidate) => candidate.name !== name);
      remaining.push({
        name,
        portability: options.sensitive ? "locked" : "portable",
        ...(entry.id ? { id: entry.id } : {})
      });
      this.inventoryCache = remaining.sort((left, right) => left.name.localeCompare(right.name));
    }
    return { verifiable: !options.sensitive && entry?.portability !== "locked" };
  }

  async remove(name: string): Promise<void> {
    if (this.config.gitBranch) throw new KeyperError("CONFIG_INVALID");
    await this.list();
    const entry = this.inventoryCache?.find((candidate) => candidate.name === name);
    if (!entry) throw new KeyperError("SOURCE_MISSING");
    const context = await this.contextArgs();
    const output = await this.processRunner("vercel", [
      "env",
      "remove",
      name,
      ...this.targetArgs(),
      "--yes",
      ...context
    ]);
    output.fill(0);
    this.inventoryCache = (this.inventoryCache ?? []).filter((entry) => entry.name !== name);
  }

  async doctor(): Promise<void> {
    await this.list();
  }
}
