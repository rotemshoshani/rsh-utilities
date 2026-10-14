import { randomUUID } from "node:crypto";
import { chmod, open, readFile, rename, unlink } from "node:fs/promises";
import { dirname, isAbsolute, resolve } from "node:path";
import { KeyperError } from "./errors.js";
import type { AliasConfig, IssuerConfig, LoadedConfig, KeyperConfig, SecretReference } from "./types.js";

const NAME_PATTERN = /^[A-Za-z][A-Za-z0-9_]*$/;
const ALIAS_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
const SSH_HOST_PATTERN = /^[A-Za-z0-9_.@:-]+$/;

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function parseFields(value: unknown): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new KeyperError("CONFIG_INVALID");
  const fields: Record<string, string> = {};
  for (const [name, pointer] of Object.entries(value as Record<string, unknown>)) {
    if (!NAME_PATTERN.test(name) || !isNonEmptyString(pointer) || !pointer.startsWith("/")) {
      throw new KeyperError("CONFIG_INVALID");
    }
    fields[name] = pointer;
  }
  if (Object.keys(fields).length === 0) throw new KeyperError("CONFIG_INVALID");
  return fields;
}

function parseReference(value: unknown): SecretReference {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const candidate = value as Record<string, unknown>;
    if (isNonEmptyString(candidate.alias) && ALIAS_PATTERN.test(candidate.alias) && isNonEmptyString(candidate.name) && NAME_PATTERN.test(candidate.name)) {
      return { alias: candidate.alias, name: candidate.name };
    }
  }
  if (!isNonEmptyString(value)) throw new KeyperError("CONFIG_INVALID");
  const separator = value.indexOf(":");
  if (separator < 1 || separator === value.length - 1) throw new KeyperError("CONFIG_INVALID");
  const alias = value.slice(0, separator);
  const name = value.slice(separator + 1);
  if (!ALIAS_PATTERN.test(alias) || !NAME_PATTERN.test(name)) throw new KeyperError("CONFIG_INVALID");
  return { alias, name };
}

function parseAlias(value: unknown): AliasConfig {
  if (!value || typeof value !== "object") throw new KeyperError("CONFIG_INVALID");
  const candidate = value as Record<string, unknown>;
  if (candidate.provider === "local" && isNonEmptyString(candidate.path)) {
    return { provider: "local", path: candidate.path };
  }
  if (candidate.provider === "convex" && isNonEmptyString(candidate.deployment)) {
    return {
      provider: "convex",
      deployment: candidate.deployment,
      ...(isNonEmptyString(candidate.cwd) ? { cwd: candidate.cwd } : {})
    };
  }
  if (
    candidate.provider === "vercel" &&
    isNonEmptyString(candidate.project) &&
    isNonEmptyString(candidate.environment)
  ) {
    return {
      provider: "vercel",
      project: candidate.project,
      environment: candidate.environment,
      ...(isNonEmptyString(candidate.scope) ? { scope: candidate.scope } : {}),
      ...(isNonEmptyString(candidate.gitBranch) ? { gitBranch: candidate.gitBranch } : {})
    };
  }
  if (
    candidate.provider === "structured" &&
    isNonEmptyString(candidate.path) &&
    candidate.fields &&
    typeof candidate.fields === "object" &&
    !Array.isArray(candidate.fields)
  ) {
    return { provider: "structured", path: candidate.path, fields: parseFields(candidate.fields) };
  }
  if (
    candidate.provider === "ssh" &&
    isNonEmptyString(candidate.host) &&
    SSH_HOST_PATTERN.test(candidate.host) &&
    isNonEmptyString(candidate.path) &&
    candidate.path.startsWith("/") &&
    (candidate.format === "dotenv" || candidate.format === "systemd")
  ) {
    return { provider: "ssh", host: candidate.host, path: candidate.path, format: candidate.format };
  }
  if (
    candidate.provider === "ssh-structured" &&
    isNonEmptyString(candidate.host) &&
    SSH_HOST_PATTERN.test(candidate.host) &&
    isNonEmptyString(candidate.path) &&
    candidate.path.startsWith("/")
  ) {
    return { provider: "ssh-structured", host: candidate.host, path: candidate.path, fields: parseFields(candidate.fields) };
  }
  if (candidate.provider === "wrangler") return { provider: "wrangler" };
  throw new KeyperError("CONFIG_INVALID");
}


function parseIssuer(value: unknown): IssuerConfig {
  if (!value || typeof value !== "object") throw new KeyperError("CONFIG_INVALID");
  const candidate = value as Record<string, unknown>;
  if (
    candidate.provider !== "cloudflare-access-service-token" ||
    !isNonEmptyString(candidate.tokenName) ||
    (candidate.duration !== undefined && !isNonEmptyString(candidate.duration))
  ) throw new KeyperError("CONFIG_INVALID");
  return {
    provider: "cloudflare-access-service-token",
    accountId: parseReference(candidate.accountId),
    apiToken: parseReference(candidate.apiToken),
    tokenName: candidate.tokenName,
    ...(candidate.duration ? { duration: candidate.duration as string } : {})
  };
}

export async function loadConfig(path = ".keyper.json"): Promise<LoadedConfig> {
  const configPath = resolve(path);
  let parsed: unknown;
  try {
    parsed = JSON.parse(await readFile(configPath, "utf8"));
  } catch {
    throw new KeyperError("CONFIG_INVALID");
  }
  if (!parsed || typeof parsed !== "object") throw new KeyperError("CONFIG_INVALID");
  const candidate = parsed as Record<string, unknown>;
  if (candidate.version !== 1 || !candidate.aliases || typeof candidate.aliases !== "object") {
    throw new KeyperError("CONFIG_INVALID");
  }
  const aliases: Record<string, AliasConfig> = {};
  for (const [name, value] of Object.entries(candidate.aliases as Record<string, unknown>)) {
    if (!ALIAS_PATTERN.test(name)) throw new KeyperError("CONFIG_INVALID");
    const alias = parseAlias(value);
    if (alias.provider === "local" || alias.provider === "structured") {
      alias.path = isAbsolute(alias.path) ? alias.path : resolve(dirname(configPath), alias.path);
    }
    if (alias.provider === "convex" && alias.cwd) {
      alias.cwd = isAbsolute(alias.cwd) ? alias.cwd : resolve(dirname(configPath), alias.cwd);
    }
    aliases[name] = alias;
  }
  if (Object.keys(aliases).length === 0) throw new KeyperError("CONFIG_INVALID");
  const issuers: Record<string, IssuerConfig> = {};
  if (candidate.issuers !== undefined) {
    if (!candidate.issuers || typeof candidate.issuers !== "object" || Array.isArray(candidate.issuers)) {
      throw new KeyperError("CONFIG_INVALID");
    }
    for (const [name, value] of Object.entries(candidate.issuers as Record<string, unknown>)) {
      if (!ALIAS_PATTERN.test(name)) throw new KeyperError("CONFIG_INVALID");
      issuers[name] = parseIssuer(value);
    }
  }
  const checks: Record<string, SecretReference[]> = {};
  if (candidate.checks !== undefined) {
    if (!candidate.checks || typeof candidate.checks !== "object" || Array.isArray(candidate.checks)) {
      throw new KeyperError("CONFIG_INVALID");
    }
    for (const [name, value] of Object.entries(candidate.checks as Record<string, unknown>)) {
      if (!ALIAS_PATTERN.test(name) || !Array.isArray(value) || value.length < 2) throw new KeyperError("CONFIG_INVALID");
      const references = value.map(parseReference);
      if (
        references.some((reference) => !aliases[reference.alias]) ||
        new Set(references.map((reference) => `${reference.alias}\0${reference.name}`)).size !== references.length
      ) throw new KeyperError("CONFIG_INVALID");
      checks[name] = references;
    }
  }
  const config: KeyperConfig = {
    version: 1,
    aliases,
    ...(Object.keys(issuers).length ? { issuers } : {}),
    ...(Object.keys(checks).length ? { checks } : {})
  };
  return { config, configPath, baseDir: dirname(configPath) };
}

export function requireName(name: string): string {
  if (!NAME_PATTERN.test(name)) throw new KeyperError("NAME_INVALID");
  return name;
}

export function requireAlias(config: KeyperConfig, alias: string): AliasConfig {
  const result = config.aliases[alias];
  if (!result) throw new KeyperError("ALIAS_NOT_FOUND");
  return result;
}

async function writeConfig(path: string, config: KeyperConfig): Promise<void> {
  const temporary = `${path}.keyper-${randomUUID()}`;
  const bytes = Buffer.from(`${JSON.stringify(config, null, 2)}\n`);
  let handle: Awaited<ReturnType<typeof open>> | undefined;
  try {
    handle = await open(temporary, "wx", 0o600);
    await handle.writeFile(bytes);
    await handle.sync();
    await handle.close();
    handle = undefined;
    await chmod(temporary, 0o600);
    await rename(temporary, path);
  } catch {
    if (handle) await handle.close().catch(() => undefined);
    await unlink(temporary).catch(() => undefined);
    throw new KeyperError("CONFIG_INVALID");
  } finally {
    bytes.fill(0);
  }
}

export async function addAlias(path: string | undefined, name: string, alias: AliasConfig): Promise<void> {
  if (!ALIAS_PATTERN.test(name)) throw new KeyperError("CONFIG_INVALID");
  const loaded = await loadConfig(path);
  if (loaded.config.aliases[name] || loaded.config.issuers?.[name]) throw new KeyperError("DESTINATION_EXISTS");
  const raw = JSON.parse(await readFile(loaded.configPath, "utf8")) as KeyperConfig;
  const candidate: KeyperConfig = { ...raw, aliases: { ...raw.aliases, [name]: alias } };
  parseAlias(alias);
  await writeConfig(loaded.configPath, candidate);
}

export async function addIssuer(path: string | undefined, name: string, issuer: IssuerConfig): Promise<void> {
  if (!ALIAS_PATTERN.test(name)) throw new KeyperError("CONFIG_INVALID");
  const loaded = await loadConfig(path);
  if (loaded.config.aliases[name] || loaded.config.issuers?.[name]) throw new KeyperError("DESTINATION_EXISTS");
  parseIssuer(issuer);
  const raw = JSON.parse(await readFile(loaded.configPath, "utf8")) as KeyperConfig;
  await writeConfig(loaded.configPath, {
    ...raw,
    issuers: { ...(raw.issuers ?? {}), [name]: issuer }
  });
}
