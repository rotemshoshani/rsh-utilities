import { lstat, readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, join, resolve } from "node:path";
import { KeyperError } from "./errors.js";
import { LocalProvider } from "./providers/local.js";
import { runSafeProcess } from "./safe-process.js";

type Schema = "public" | "sensitive" | { readonly [field: string]: Schema } | readonly [Schema];
type Runner = typeof runSafeProcess;

const scalarArray = ["public"] as const;
const timestamp = "public" as const;

const projectSchema: Schema = [{
  Name: "public", ProjectId: "public", URL: "public",
  APIKey: "sensitive", APISecret: "sensitive"
}];
const inboundTrunkSchema: Schema = {
  sipTrunkId: "public", name: "public", metadata: "sensitive", numbers: scalarArray,
  allowedAddresses: scalarArray, allowedNumbers: scalarArray,
  authUsername: "sensitive", authPassword: "sensitive", authRealm: "sensitive",
  headers: "sensitive", headersToAttributes: "sensitive", attributesToHeaders: "sensitive",
  includeHeaders: "public", ringingTimeout: "public", maxCallDuration: "public",
  krispEnabled: "public", mediaEncryption: "public", media: "sensitive",
  createdAt: timestamp, updatedAt: timestamp
};
const outboundTrunkSchema: Schema = {
  sipTrunkId: "public", name: "public", metadata: "sensitive", address: "public",
  destinationCountry: "public", transport: "public", numbers: scalarArray,
  authUsername: "sensitive", authPassword: "sensitive", headers: "sensitive",
  headersToAttributes: "sensitive", attributesToHeaders: "sensitive", includeHeaders: "public",
  mediaEncryption: "public", media: "sensitive", fromHost: "public",
  createdAt: timestamp, updatedAt: timestamp
};
const dispatchSchema: Schema = {
  sipDispatchRuleId: "public",
  rule: {
    dispatchRuleDirect: { roomName: "public", pin: "sensitive" },
    dispatchRuleIndividual: { roomPrefix: "public", pin: "sensitive", noRandomness: "public" },
    dispatchRuleCallee: { roomPrefix: "public", pin: "sensitive", randomize: "public" }
  },
  trunkIds: scalarArray, hidePhoneNumber: "public", inboundNumbers: scalarArray,
  numbers: scalarArray, name: "public", metadata: "sensitive", attributes: "sensitive",
  roomPreset: "public",
  roomConfig: {
    name: "sensitive", emptyTimeout: "sensitive", departureTimeout: "sensitive",
    maxParticipants: "sensitive", metadata: "sensitive", egress: "sensitive",
    minPlayoutDelay: "sensitive", maxPlayoutDelay: "sensitive", syncStreams: "sensitive",
    agents: [{
      agentName: "public", metadata: "sensitive", restartPolicy: "sensitive",
      deployment: "sensitive", attributes: "sensitive"
    }],
    tags: "sensitive"
  },
  media: "sensitive",
  krispEnabled: "public", mediaEncryption: "public", createdAt: timestamp, updatedAt: timestamp
};
const phoneSchema: Schema = {
  items: [{
    id: "public", e164Format: "public", countryCode: "public", areaCode: "public",
    numberType: "public", locality: "public", region: "public", spamScore: "public",
    createdAt: timestamp, updatedAt: timestamp, capabilities: scalarArray, status: "public",
    assignedAt: timestamp, releasedAt: timestamp, sipDispatchRuleId: "public",
    sipDispatchRuleIds: scalarArray
  }],
  nextPageToken: "sensitive", totalCount: "public", offlineCount: "public"
};

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function sanitize(value: unknown, schema: Schema): unknown {
  if (schema === "sensitive") return undefined;
  if (schema === "public") {
    if (value !== null && !["string", "number", "boolean"].includes(typeof value)) throw new KeyperError("PROVIDER_SCHEMA");
    return value;
  }
  if (Array.isArray(schema)) {
    if (!Array.isArray(value)) throw new KeyperError("PROVIDER_SCHEMA");
    return value.map((item) => sanitize(item, schema[0]));
  }
  if (!object(value)) throw new KeyperError("PROVIDER_SCHEMA");
  const fields = schema as { readonly [field: string]: Schema };
  if (Object.keys(value).some((field) => !Object.hasOwn(fields, field))) throw new KeyperError("PROVIDER_SCHEMA");
  const safe: Record<string, unknown> = {};
  for (const [field, item] of Object.entries(value)) {
    const sanitized = sanitize(item, fields[field]!);
    if (sanitized !== undefined) safe[field] = sanitized;
  }
  return safe;
}

export function safeProviderJson(output: Buffer, schema: Schema): unknown {
  try {
    return sanitize(JSON.parse(output.toString("utf8")) as unknown, schema);
  } catch (error: unknown) {
    if (error instanceof KeyperError) throw error;
    throw new KeyperError("PROVIDER_SCHEMA");
  } finally {
    output.fill(0);
  }
}

function identifier(value: string): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(value)) throw new KeyperError("PROVIDER_ARGUMENTS");
  return value;
}

function target(path: string): string {
  const resolved = resolve(path);
  if (basename(resolved) !== ".env.local") throw new KeyperError("PROVIDER_ARGUMENTS");
  return resolved;
}

async function captured(runner: Runner, command: string, args: readonly string[]): Promise<void> {
  const output = await runner(command, args);
  output.fill(0);
}

async function writeCredentials(path: string, values: ReadonlyArray<readonly [string, string]>, overwrite: boolean): Promise<void> {
  const buffers = values.map(([name, value]) => [name, Buffer.from(value, "utf8")] as const);
  try {
    await new LocalProvider(target(path)).writeMany(buffers, { overwrite, sensitive: false });
  } finally {
    for (const [, value] of buffers) value.fill(0);
  }
}

function parseTsv(output: Buffer, fields: readonly string[]): Array<Record<string, string>> {
  try {
    const text = output.toString("utf8").replace(/\r\n/g, "\n").replace(/\n$/, "");
    if (!text) return [];
    const rows = text.split("\n").map((line) => line.split("\t"));
    if (rows[0]?.length !== fields.length || rows[0]?.some((field, index) => field !== fields[index])) {
      throw new KeyperError("PROVIDER_SCHEMA");
    }
    return rows.slice(1).map((row) => {
      if (row.length !== fields.length) throw new KeyperError("PROVIDER_SCHEMA");
      return Object.fromEntries(fields.map((field, index) => [field, row[index] ?? ""]));
    });
  } finally {
    output.fill(0);
  }
}

const twilioLists = {
  accounts: { command: "api:core:accounts:list", fields: ["sid", "status", "type"] },
  trunks: { command: "api:trunking:v1:trunks:list", fields: ["sid", "domainName"] },
  numbers: { command: "phone-numbers:list", fields: ["sid", "phoneNumber", "trunkSid"] },
  routing: { command: "phone-numbers:list", fields: ["sid", "phoneNumber", "trunkSid", "voiceMethod", "smsMethod"] }
} as const;

type TwilioConfig = {
  activeProject?: string | null;
  profiles: Record<string, { accountSid: string; region?: string | null; edge?: string | null; apiKey: string; apiSecret: string }>;
};

async function twilioConfig(path = join(homedir(), ".twilio-cli", "config.json")): Promise<TwilioConfig> {
  let raw: Buffer | undefined;
  try {
    const stat = await lstat(path);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 16 * 1024 * 1024) throw new KeyperError("PROVIDER_SCHEMA");
    raw = await readFile(path);
    const value = JSON.parse(raw.toString("utf8")) as unknown;
    if (!object(value) || !object(value.profiles)) throw new KeyperError("PROVIDER_SCHEMA");
    if (value.activeProject !== undefined && value.activeProject !== null && typeof value.activeProject !== "string") {
      throw new KeyperError("PROVIDER_SCHEMA");
    }
    for (const profile of Object.values(value.profiles)) {
      if (!object(profile)) throw new KeyperError("PROVIDER_SCHEMA");
      if (!["accountSid", "apiKey", "apiSecret"].every((field) => typeof profile[field] === "string")) throw new KeyperError("PROVIDER_SCHEMA");
      if (["region", "edge"].some((field) => profile[field] !== undefined && profile[field] !== null && typeof profile[field] !== "string")) {
        throw new KeyperError("PROVIDER_SCHEMA");
      }
    }
    return value as TwilioConfig;
  } catch (error: unknown) {
    if (error instanceof KeyperError) throw error;
    throw new KeyperError("PROVIDER_SCHEMA");
  } finally {
    raw?.fill(0);
  }
}

export async function listTwilio(kind: "profiles" | keyof typeof twilioLists, profile?: string, runner: Runner = runSafeProcess, configPath?: string): Promise<unknown> {
  if (kind === "profiles") {
    if (profile) throw new KeyperError("PROVIDER_ARGUMENTS");
    const config = await twilioConfig(configPath);
    return Object.entries(config.profiles).map(([id, item]) => ({
      id, accountSid: item.accountSid, ...(item.region ? { region: item.region } : {}),
      ...(item.edge ? { edge: item.edge } : {}), active: config.activeProject === id
    }));
  }
  const list = twilioLists[kind];
  const args = [list.command, "-o", "tsv", "--properties", list.fields.join(",")];
  if (profile) args.push("--profile", identifier(profile));
  return parseTsv(await runner("twilio", args), list.fields);
}

export async function useTwilio(profile: string, runner: Runner = runSafeProcess): Promise<void> {
  await captured(runner, "twilio", ["profiles:use", identifier(profile)]);
}

export async function importTwilio(profile: string, path: string, overwrite: boolean, configPath?: string): Promise<void> {
  const item = (await twilioConfig(configPath)).profiles[identifier(profile)];
  if (!item) throw new KeyperError("SOURCE_MISSING");
  await writeCredentials(path, [
    ["TWILIO_ACCOUNT_SID", item.accountSid], ["TWILIO_API_KEY", item.apiKey], ["TWILIO_API_SECRET", item.apiSecret],
    ...(item.region ? [["TWILIO_REGION", item.region] as const] : []),
    ...(item.edge ? [["TWILIO_EDGE", item.edge] as const] : [])
  ], overwrite);
}

export async function listLiveKit(kind: "projects" | "trunks" | "numbers" | "routing", project?: string, runner: Runner = runSafeProcess): Promise<unknown> {
  const prefix = project ? ["--project", identifier(project)] : [];
  if (kind === "projects") {
    if (project) throw new KeyperError("PROVIDER_ARGUMENTS");
    return safeProviderJson(await runner("lk", ["project", "list", "--json"]), projectSchema);
  }
  if (kind === "trunks") {
    const inbound = safeProviderJson(await runner("lk", [...prefix, "sip", "inbound", "list", "--json"]), { items: [inboundTrunkSchema] });
    const outbound = safeProviderJson(await runner("lk", [...prefix, "sip", "outbound", "list", "--json"]), { items: [outboundTrunkSchema] });
    return { inbound: (inbound as { items: unknown[] }).items, outbound: (outbound as { items: unknown[] }).items };
  }
  const command = kind === "numbers" ? ["number", "list", "--json"] : ["sip", "dispatch", "list", "--json"];
  const schema: Schema = kind === "numbers" ? phoneSchema : { items: [dispatchSchema] as const };
  return safeProviderJson(await runner("lk", [...prefix, ...command]), schema);
}

export async function useLiveKit(project: string, runner: Runner = runSafeProcess): Promise<void> {
  await captured(runner, "lk", ["project", "set-default", identifier(project)]);
}

export async function importLiveKit(project: string, path: string, overwrite: boolean, runner: Runner = runSafeProcess): Promise<void> {
  const output = await runner("lk", ["project", "list", "--json"]);
  let raw: unknown;
  try {
    raw = JSON.parse(output.toString("utf8")) as unknown;
    sanitize(raw, projectSchema);
  } catch (error: unknown) {
    if (error instanceof KeyperError) throw error;
    throw new KeyperError("PROVIDER_SCHEMA");
  } finally {
    output.fill(0);
  }
  const item = (raw as Array<Record<string, unknown>>).find((candidate) => candidate.Name === identifier(project));
  if (!item) throw new KeyperError("SOURCE_MISSING");
  if (![item.URL, item.APIKey, item.APISecret].every((value) => typeof value === "string")) throw new KeyperError("PROVIDER_SCHEMA");
  await writeCredentials(path, [
    ["LIVEKIT_URL", item.URL as string], ["LIVEKIT_API_KEY", item.APIKey as string], ["LIVEKIT_API_SECRET", item.APISecret as string]
  ], overwrite);
}
