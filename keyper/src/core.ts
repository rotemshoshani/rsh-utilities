import { randomBytes, timingSafeEqual } from "node:crypto";
import { requireAlias, requireName, requireTool } from "./config.js";
import { KeyperError } from "./errors.js";
import { fileGitStatus, type FileGitStatus } from "./git-status.js";
import { createProvider } from "./provider.js";
import { assertSafeTool, environmentValue, runTool, type ToolRunResult } from "./run.js";
import { issueCloudflareAccessServiceToken, probeCloudflare, type CloudflareProbeResult } from "./issuers/cloudflare-access-service-token.js";
import type { InventoryEntry, KeyperConfig, SecretReference } from "./types.js";

export interface CopyRequest {
  name: string;
  from: string;
  to: string;
  toName?: string;
  overwrite: boolean;
  sensitive: boolean;
}

export interface MoveRequest extends CopyRequest {
  confirmDeleteSource: string;
  acceptUnverifiedSourceDeletion: boolean;
}

export interface Destination {
  alias: string;
  name?: string;
  sensitive?: boolean;
}

export interface CopyManyRequest {
  name: string;
  from: string;
  destinations: Destination[];
  overwrite: boolean;
}

export interface ComposeSource {
  alias: string;
  name: string;
}

export type IssuerOutput = "CLIENT_ID" | "CLIENT_SECRET" | "RESOURCE_ID";

export interface IssueDestination extends Destination {
  output: IssuerOutput;
}

export type FanoutStatus = "verified" | "unverified" | "failed" | "rolled-back" | "rollback-failed" | "not-attempted";

export interface FanoutResult {
  ok: boolean;
  destinations: Array<{ alias: string; name: string; status: FanoutStatus }>;
}

export type CompareResult = "equal" | "different" | "missing" | "locked";
export type AuditStatus = CompareResult | "failed";

export type ToolStatus = "ok" | "unsafe" | "missing" | "locked" | "failed";

export interface DoctorReport {
  aliases: Record<string, "ok" | "failed">;
  files: Record<string, FileGitStatus>;
  tools: Record<string, ToolStatus>;
}

export interface ToolResult extends ToolRunResult {
  injected: string[];
}

export type ToolRunner = typeof runTool;

export type ProviderFactory = (config: Parameters<typeof createProvider>[0]) => ReturnType<typeof createProvider>;

function equalBuffers(left: Buffer, right: Buffer): boolean {
  return left.length === right.length && timingSafeEqual(left, right);
}

function sameLocation(left: ReturnType<typeof requireAlias>, right: ReturnType<typeof requireAlias>): boolean {
  if (left.provider !== right.provider) return false;
  if (left.provider === "local" && right.provider === "local") return left.path === right.path;
  if (left.provider === "convex" && right.provider === "convex") {
    return left.deployment === right.deployment && left.cwd === right.cwd;
  }
  if (left.provider === "vercel" && right.provider === "vercel") {
    return left.project === right.project &&
      left.scope === right.scope &&
      left.environment === right.environment &&
      left.gitBranch === right.gitBranch;
  }
  if (left.provider === "structured" && right.provider === "structured") return left.path === right.path;
  if (left.provider === "ssh" && right.provider === "ssh") {
    return left.host === right.host && left.path === right.path && left.format === right.format;
  }
  if (left.provider === "ssh-structured" && right.provider === "ssh-structured") {
    return left.host === right.host && left.path === right.path;
  }
  if (left.provider === "wrangler" && right.provider === "wrangler") return true;
  return false;
}

export class Keyper {
  private readonly providers = new Map<string, ReturnType<ProviderFactory>>();

  constructor(
    private readonly config: KeyperConfig,
    private readonly providerFactory: ProviderFactory = createProvider,
    private readonly toolRunner: ToolRunner = runTool
  ) {}

  private provider(alias: string): ReturnType<ProviderFactory> {
    const existing = this.providers.get(alias);
    if (existing) return existing;
    const created = this.providerFactory(requireAlias(this.config, alias));
    this.providers.set(alias, created);
    return created;
  }

  async inventory(alias: string): Promise<InventoryEntry[]> {
    return await this.provider(alias).list();
  }

  async inventoryAll(): Promise<Record<string, InventoryEntry[] | "failed">> {
    const result: Record<string, InventoryEntry[] | "failed"> = {};
    for (const alias of Object.keys(this.config.aliases).sort()) {
      try {
        result[alias] = await this.inventory(alias);
      } catch {
        result[alias] = "failed";
      }
    }
    return result;
  }

  async compare(name: string, leftAlias: string, rightAlias: string, rightName = name): Promise<CompareResult> {
    requireName(name);
    requireName(rightName);
    const left = this.provider(leftAlias);
    const right = this.provider(rightAlias);
    const leftEntry = (await left.list()).find((entry) => entry.name === name);
    const rightEntry = (await right.list()).find((entry) => entry.name === rightName);
    if (!leftEntry || !rightEntry) return "missing";
    if (leftEntry.portability === "locked" || rightEntry.portability === "locked") return "locked";
    let leftValue: Buffer | undefined;
    let rightValue: Buffer | undefined;
    try {
      leftValue = await left.read(name);
      rightValue = await right.read(rightName);
      return equalBuffers(leftValue, rightValue) ? "equal" : "different";
    } finally {
      leftValue?.fill(0);
      rightValue?.fill(0);
    }
  }

  async audit(references: SecretReference[]): Promise<AuditStatus> {
    if (references.length < 2) throw new KeyperError("CONFIG_INVALID");
    for (const reference of references) {
      requireAlias(this.config, reference.alias);
      requireName(reference.name);
    }
    const values: Buffer[] = [];
    try {
      const inventories = new Map<string, InventoryEntry[]>();
      const entries: Array<InventoryEntry | undefined> = [];
      for (const reference of references) {
        let inventory = inventories.get(reference.alias);
        if (!inventory) {
          inventory = await this.provider(reference.alias).list();
          inventories.set(reference.alias, inventory);
        }
        entries.push(inventory.find((entry) => entry.name === reference.name));
      }
      if (entries.some((entry) => !entry)) return "missing";
      if (entries.some((entry) => entry?.portability === "locked")) return "locked";
      for (const reference of references) values.push(await this.provider(reference.alias).read(reference.name));
      return values.slice(1).every((value) => equalBuffers(values[0]!, value)) ? "equal" : "different";
    } catch {
      return "failed";
    } finally {
      for (const value of values) value.fill(0);
    }
  }

  private async writeValue(
    value: Buffer,
    alias: string,
    name: string,
    options: { overwrite: boolean; sensitive: boolean; valueType?: "string" | "boolean" | "json" },
    afterWrite?: () => void
  ): Promise<boolean> {
    const destinationConfig = requireAlias(this.config, alias);
    if (options.sensitive && destinationConfig.provider !== "vercel") {
      throw new KeyperError("CONFIG_INVALID");
    }
    const destination = this.provider(alias);
    let verification: Buffer | undefined;
    try {
      const { verifiable } = await destination.write(name, value, options);
      afterWrite?.();
      if (!verifiable) return false;
      verification = await destination.read(name);
      if (!equalBuffers(value, verification)) throw new KeyperError("VALUES_DIFFER");
      return true;
    } finally {
      verification?.fill(0);
    }
  }

  async fanout(
    name: string,
    value: Buffer,
    destinations: Destination[],
    overwrite: boolean,
    valueType: "string" | "boolean" | "json" = "string",
    source?: { alias: string; name: string }
  ): Promise<FanoutResult> {
    requireName(name);
    if (destinations.length === 0) throw new KeyperError("CONFIG_INVALID");
    const targets = destinations.map((destination) => ({
      alias: destination.alias,
      name: requireName(destination.name ?? name),
      sensitive: destination.sensitive ?? false
    }));
    for (let index = 0; index < targets.length; index += 1) {
      const target = targets[index]!;
      const config = requireAlias(this.config, target.alias);
      if (targets.slice(0, index).some((previous) =>
        previous.name === target.name && sameLocation(requireAlias(this.config, previous.alias), config)
      )) throw new KeyperError("SAME_LOCATION");
      if (source && source.name === target.name && sameLocation(requireAlias(this.config, source.alias), config)) {
        throw new KeyperError("SAME_LOCATION");
      }
      if (target.sensitive && config.provider !== "vercel") throw new KeyperError("CONFIG_INVALID");
    }

    const snapshots: Array<{ existed: boolean; value?: Buffer; portable: boolean }> = [];
    try {
      for (const target of targets) {
        const provider = this.provider(target.alias);
        const entry = (await provider.list()).find((candidate) => candidate.name === target.name);
        if (entry && !overwrite) throw new KeyperError("DESTINATION_EXISTS");
        snapshots.push({
          existed: Boolean(entry),
          portable: entry?.portability !== "locked",
          ...(entry?.portability === "portable" ? { value: await provider.read(target.name) } : {})
        });
      }

      const statuses = targets.map((target) => ({ ...target, status: "not-attempted" as FanoutStatus }));
      let completed = 0;
      let currentWritten = false;
      try {
        for (; completed < targets.length; completed += 1) {
          const target = targets[completed]!;
          currentWritten = false;
          const verified = await this.writeValue(value, target.alias, target.name, {
            overwrite,
            sensitive: target.sensitive,
            valueType
          }, () => { currentWritten = true; });
          statuses[completed]!.status = verified ? "verified" : "unverified";
        }
        return { ok: true, destinations: statuses.map(({ alias, name: targetName, status }) => ({ alias, name: targetName, status })) };
      } catch {
        if (completed < statuses.length) statuses[completed]!.status = "failed";
        for (let index = currentWritten ? completed : completed - 1; index >= 0; index -= 1) {
          const target = targets[index]!;
          const snapshot = snapshots[index]!;
          try {
            if (!snapshot.portable) {
              statuses[index]!.status = "rollback-failed";
            } else if (snapshot.existed && snapshot.value) {
              await this.provider(target.alias).write(target.name, snapshot.value, {
                overwrite: true,
                sensitive: false,
                valueType: "string"
              });
              if (index !== completed) statuses[index]!.status = "rolled-back";
            } else {
              await this.provider(target.alias).remove(target.name);
              if (index !== completed) statuses[index]!.status = "rolled-back";
            }
          } catch {
            statuses[index]!.status = "rollback-failed";
          }
        }
        return { ok: false, destinations: statuses.map(({ alias, name: targetName, status }) => ({ alias, name: targetName, status })) };
      }
    } finally {
      for (const snapshot of snapshots) snapshot.value?.fill(0);
    }
  }

  async copyMany(request: CopyManyRequest): Promise<FanoutResult> {
    const name = requireName(request.name);
    let value: Buffer | undefined;
    try {
      value = await this.provider(request.from).read(name);
      return await this.fanout(name, value, request.destinations, request.overwrite, "string", {
        alias: request.from,
        name
      });
    } finally {
      value?.fill(0);
    }
  }

  async generate(name: string, destinations: Destination[], overwrite: boolean, bytes = 32): Promise<FanoutResult> {
    if (!Number.isSafeInteger(bytes) || bytes < 16 || bytes > 4096) throw new KeyperError("CONFIG_INVALID");
    const random = randomBytes(bytes);
    const value = Buffer.from(random.toString("base64url"), "utf8");
    random.fill(0);
    try {
      return await this.fanout(name, value, destinations, overwrite);
    } finally {
      value.fill(0);
    }
  }

  async compose(
    name: string,
    sources: ComposeSource[],
    format: "csv" | "json",
    destinations: Destination[],
    overwrite: boolean
  ): Promise<FanoutResult> {
    if (sources.length === 0) throw new KeyperError("CONFIG_INVALID");
    const values: Buffer[] = [];
    let composed: Buffer | undefined;
    try {
      for (const source of sources) values.push(await this.provider(source.alias).read(requireName(source.name)));
      const strings = values.map((value) => value.toString("utf8"));
      const text = format === "json"
        ? JSON.stringify(strings)
        : strings.map((value) => /[",\r\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value).join(",");
      composed = Buffer.from(text, "utf8");
      return await this.fanout(name, composed, destinations, overwrite, format === "json" ? "json" : "string");
    } finally {
      composed?.fill(0);
      for (const value of values) value.fill(0);
    }
  }

  async put(
    name: string,
    value: Buffer,
    destinations: Destination[],
    overwrite: boolean,
    valueType: "string" | "boolean" | "json" = "string"
  ): Promise<FanoutResult> {
    return await this.fanout(name, value, destinations, overwrite, valueType);
  }

  async issue(issuerName: string, destinations: IssueDestination[]): Promise<FanoutResult> {
    const issuer = this.config.issuers?.[issuerName];
    if (!issuer) throw new KeyperError("ALIAS_NOT_FOUND");
    const outputs: IssuerOutput[] = ["CLIENT_ID", "CLIENT_SECRET", "RESOURCE_ID"];
    if (outputs.some((output) => !destinations.some((destination) => destination.output === output))) {
      throw new KeyperError("ISSUER_OUTPUTS_REQUIRED");
    }
    for (const destination of destinations) {
      const name = requireName(destination.name ?? destination.output);
      const existing = await this.provider(destination.alias).list();
      if (existing.some((entry) => entry.name === name)) throw new KeyperError("DESTINATION_EXISTS");
    }

    let accountId: Buffer | undefined;
    let apiToken: Buffer | undefined;
    let issued: Awaited<ReturnType<typeof issueCloudflareAccessServiceToken>> | undefined;
    const completed: IssueDestination[] = [];
    const statuses: FanoutResult["destinations"] = [];
    try {
      accountId = await this.provider(issuer.accountId.alias).read(requireName(issuer.accountId.name));
      apiToken = await this.provider(issuer.apiToken.alias).read(requireName(issuer.apiToken.name));
      issued = await issueCloudflareAccessServiceToken(issuer, accountId, apiToken);
      const values: Record<IssuerOutput, Buffer> = {
        CLIENT_ID: issued.clientId,
        CLIENT_SECRET: issued.clientSecret,
        RESOURCE_ID: issued.resourceId
      };
      for (const output of outputs) {
        const targets = destinations.filter((destination) => destination.output === output);
        const result = await this.fanout(output, values[output], targets, false);
        statuses.push(...result.destinations);
        if (!result.ok) throw new KeyperError("FANOUT_PARTIAL");
        completed.push(...targets);
      }
      return { ok: true, destinations: statuses };
    } catch (error) {
      for (const destination of completed.reverse()) {
        const name = requireName(destination.name ?? destination.output);
        try {
          await this.provider(destination.alias).remove(name);
          const status = statuses.find((entry) => entry.alias === destination.alias && entry.name === name);
          if (status) status.status = "rolled-back";
        } catch {
          const status = statuses.find((entry) => entry.alias === destination.alias && entry.name === name);
          if (status) status.status = "rollback-failed";
        }
      }
      if (issued) await issued.revoke().catch(() => undefined);
      if (statuses.length) return { ok: false, destinations: statuses };
      throw error;
    } finally {
      accountId?.fill(0);
      apiToken?.fill(0);
      issued?.clientId.fill(0);
      issued?.clientSecret.fill(0);
      issued?.resourceId.fill(0);
    }
  }

  async cloudflareDoctor(issuerName: string, zoneName: string): Promise<CloudflareProbeResult> {
    const issuer = this.config.issuers?.[issuerName];
    if (!issuer) throw new KeyperError("ALIAS_NOT_FOUND");
    let accountId: Buffer | undefined;
    let apiToken: Buffer | undefined;
    try {
      accountId = await this.provider(issuer.accountId.alias).read(requireName(issuer.accountId.name));
      apiToken = await this.provider(issuer.apiToken.alias).read(requireName(issuer.apiToken.name));
      return await probeCloudflare(accountId, apiToken, zoneName);
    } finally {
      accountId?.fill(0);
      apiToken?.fill(0);
    }
  }

  async copy(request: CopyRequest): Promise<{ verified: boolean; destinationName: string }> {
    const sourceName = requireName(request.name);
    const destinationName = requireName(request.toName ?? request.name);
    const sourceConfig = requireAlias(this.config, request.from);
    const destinationConfig = requireAlias(this.config, request.to);
    if (sourceName === destinationName && sameLocation(sourceConfig, destinationConfig)) {
      throw new KeyperError("SAME_LOCATION");
    }
    const source = this.provider(request.from);
    const destination = this.provider(request.to);
    let value: Buffer | undefined;
    let snapshot: Buffer | undefined;
    let existed = false;
    let writeCompleted = false;
    try {
      const existing = (await destination.list()).find((entry) => entry.name === destinationName);
      existed = Boolean(existing);
      if (existing && !request.overwrite) throw new KeyperError("DESTINATION_EXISTS");
      if (existing?.portability === "portable") snapshot = await destination.read(destinationName);

      value = await source.read(sourceName);
      try {
        const verified = await this.writeValue(value, request.to, destinationName, {
          overwrite: request.overwrite,
          sensitive: request.sensitive
        }, () => { writeCompleted = true; });
        return { verified, destinationName };
      } catch (error) {
        if (writeCompleted) {
          try {
            if (existed && snapshot) {
              const restored = await destination.write(destinationName, snapshot, {
                overwrite: true,
                sensitive: false,
                valueType: "string"
              });
              if (!restored.verifiable) throw new KeyperError("FANOUT_PARTIAL");
              const restoredValue = await destination.read(destinationName);
              try {
                if (!equalBuffers(snapshot, restoredValue)) throw new KeyperError("FANOUT_PARTIAL");
              } finally {
                restoredValue.fill(0);
              }
            } else if (!existed) {
              await destination.remove(destinationName);
              if ((await destination.list()).some((entry) => entry.name === destinationName)) {
                throw new KeyperError("FANOUT_PARTIAL");
              }
            } else {
              throw new KeyperError("DESTINATION_LOCKED");
            }
          } catch {
            throw new KeyperError("FANOUT_PARTIAL");
          }
        }
        throw error;
      }
    } finally {
      value?.fill(0);
      snapshot?.fill(0);
    }
  }

  async move(request: MoveRequest): Promise<{ verified: boolean; destinationName: string }> {
    if (request.confirmDeleteSource !== request.name) {
      throw new KeyperError("CONFIRMATION_REQUIRED");
    }
    const destinationConfig = requireAlias(this.config, request.to);
    if (
      request.sensitive &&
      destinationConfig.provider === "vercel" &&
      !request.acceptUnverifiedSourceDeletion
    ) {
      throw new KeyperError("UNVERIFIED_DELETE_REQUIRES_ACK");
    }
    const result = await this.copy(request);
    if (!result.verified && !request.acceptUnverifiedSourceDeletion) {
      throw new KeyperError("UNVERIFIED_DELETE_REQUIRES_ACK");
    }
    const source = this.provider(request.from);
    await source.remove(requireName(request.name));
    return result;
  }

  async remove(name: string, from: string, confirmDelete: string): Promise<void> {
    const sourceName = requireName(name);
    if (confirmDelete !== sourceName) throw new KeyperError("CONFIRMATION_REQUIRED");
    await this.provider(from).remove(sourceName);
  }

  async run(name: string, args: readonly string[]): Promise<ToolResult> {
    const tool = requireTool(this.config, name);
    assertSafeTool(this.config, tool, args);
    const injected = Object.keys(tool.env).sort();
    const values: Buffer[] = [];
    try {
      const env: Record<string, string> = {};
      for (const variable of injected) {
        const reference = tool.env[variable]!;
        const value = await this.provider(reference.alias).read(requireName(reference.name));
        values.push(value);
        env[variable] = environmentValue(value);
      }
      const result = await this.toolRunner({
        command: tool.command,
        args,
        cwd: tool.cwd,
        env,
        secrets: values,
        timeoutMs: tool.timeoutSeconds * 1000,
        maxOutputBytes: tool.maxOutputBytes
      });
      return { ...result, injected };
    } finally {
      for (const value of values) value.fill(0);
    }
  }

  async doctor(): Promise<DoctorReport> {
    const aliases: DoctorReport["aliases"] = {};
    const files: DoctorReport["files"] = {};
    const tools: DoctorReport["tools"] = {};
    const inventories = new Map<string, InventoryEntry[] | undefined>();
    for (const alias of Object.keys(this.config.aliases).sort()) {
      try {
        await this.provider(alias).doctor();
        aliases[alias] = "ok";
      } catch {
        aliases[alias] = "failed";
      }
      const config = this.config.aliases[alias]!;
      if (config.provider === "local" || config.provider === "structured") files[alias] = await fileGitStatus(config.path);
    }
    for (const name of Object.keys(this.config.tools ?? {}).sort()) {
      const tool = this.config.tools![name]!;
      try {
        assertSafeTool(this.config, tool);
      } catch {
        tools[name] = "unsafe";
        continue;
      }
      let status: ToolStatus = "ok";
      for (const reference of Object.values(tool.env)) {
        if (!inventories.has(reference.alias)) {
          inventories.set(reference.alias, await this.inventory(reference.alias).catch(() => undefined));
        }
        const entry = inventories.get(reference.alias)?.find((candidate) => candidate.name === reference.name);
        const current: ToolStatus = !inventories.get(reference.alias) ? "failed" : !entry ? "missing" : entry.portability === "locked" ? "locked" : "ok";
        if (current !== "ok") {
          status = current;
          break;
        }
      }
      tools[name] = status;
    }
    return { aliases, files, tools };
  }
}
