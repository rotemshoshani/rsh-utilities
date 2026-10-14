#!/usr/bin/env node
import { addAlias, addIssuer, loadConfig, requireAlias, requireName } from "./config.js";
import { asSafeError, KeyperError } from "./errors.js";
import { installGuardrails } from "./guardrails.js";
import { Keyper, type Destination, type FanoutResult, type IssueDestination, type IssuerOutput } from "./core.js";
import { importLiveKit, importTwilio, listLiveKit, listTwilio, useLiveKit, useTwilio } from "./provider-tools.js";
import type { AliasConfig, IssuerConfig, KeyperConfig } from "./types.js";

const USAGE = `Usage:
  keyper list [--source ALIAS | --all] [--json] [--config PATH]
  keyper copy NAME --from ALIAS --to ALIAS [--to-name NAME] [--overwrite] [--sensitive] [--config PATH]
  keyper copy-many NAME --from ALIAS --to ALIAS[:NAME] [--to ALIAS[:NAME] ...] [--overwrite] [--sensitive] [--config PATH]
  keyper compare NAME --between ALIAS ALIAS [--config PATH]
  keyper compare ALIAS:NAME ALIAS:NAME [--config PATH]
  keyper audit (CHECK | --all) [--json] [--config PATH]
  keyper sync CHECK --from ALIAS:NAME [--overwrite] [--config PATH]
  keyper move NAME --from ALIAS --to ALIAS --confirm-delete-source NAME [options]
  keyper remove NAME --from ALIAS --confirm-delete NAME [--config PATH]
  keyper generate NAME --to ALIAS[:NAME] [--to ALIAS[:NAME] ...] [--bytes N] [options]
  keyper compose NAME --from ALIAS:NAME --from ALIAS:NAME --format csv|json --to ALIAS[:NAME] [options]
  keyper put NAME --stdin --to ALIAS[:NAME] [--to ALIAS[:NAME] ...] [options]
  keyper enable|disable NAME --to ALIAS[:NAME] [--to ALIAS[:NAME] ...] [options]
  keyper issue ISSUER --to CLIENT_ID=ALIAS:NAME --to CLIENT_SECRET=ALIAS:NAME --to RESOURCE_ID=ALIAS:NAME [--config PATH]
  keyper alias-add NAME --provider TYPE [provider options] [--config PATH]
  keyper issuer-add NAME --provider cloudflare-access-service-token [provider options] [--config PATH]
  keyper plan COMMAND ...
  keyper doctor [--json] [--config PATH]
  keyper cloudflare-doctor ISSUER --zone DOMAIN [--json] [--config PATH]
  keyper twilio list profiles|accounts|trunks|numbers|routing [--profile NAME]
  keyper twilio use PROFILE
  keyper twilio import PROFILE --to PATH/.env.local [--overwrite]
  keyper livekit list projects|trunks|numbers|routing [--project NAME]
  keyper livekit use PROJECT
  keyper livekit import PROJECT --to PATH/.env.local [--overwrite]
  keyper install-rules (--project PATH | --global)`;

const HELP: Record<string, string> = {
  list: "keyper list [--source ALIAS | --all] [--json] [--config PATH]",
  copy: "keyper copy NAME --from ALIAS --to ALIAS [--to-name NAME] [--overwrite] [--sensitive] [--config PATH]",
  "copy-many": "keyper copy-many NAME --from ALIAS --to ALIAS[:NAME] [--to ALIAS[:NAME] ...] [--overwrite] [--sensitive] [--config PATH]",
  compare: "keyper compare NAME --between ALIAS ALIAS [--config PATH]\nkeyper compare ALIAS:NAME ALIAS:NAME [--config PATH]",
  audit: "keyper audit (CHECK | --all) [--json] [--config PATH]",
  sync: "keyper sync CHECK --from ALIAS:NAME [--overwrite] [--config PATH]",
  move: "keyper move NAME --from ALIAS --to ALIAS --confirm-delete-source NAME [--to-name NAME] [--overwrite] [--sensitive] [--accept-unverified-source-deletion] [--config PATH]",
  remove: "keyper remove NAME --from ALIAS --confirm-delete NAME [--config PATH]",
  generate: "keyper generate NAME --to ALIAS[:NAME] [--to ALIAS[:NAME] ...] [--bytes 16..4096] [--overwrite] [--sensitive] [--config PATH]",
  compose: "keyper compose NAME --from ALIAS:NAME --from ALIAS:NAME --format csv|json --to ALIAS[:NAME] [--to ALIAS[:NAME] ...] [--overwrite] [--sensitive] [--config PATH]",
  put: "keyper put NAME --stdin --to ALIAS[:NAME] [--to ALIAS[:NAME] ...] [--overwrite] [--sensitive] [--config PATH]",
  enable: "keyper enable NAME --to ALIAS[:NAME] [--to ALIAS[:NAME] ...] [--overwrite] [--config PATH]",
  disable: "keyper disable NAME --to ALIAS[:NAME] [--to ALIAS[:NAME] ...] [--overwrite] [--config PATH]",
  issue: "keyper issue ISSUER --to CLIENT_ID=ALIAS:NAME --to CLIENT_SECRET=ALIAS:NAME --to RESOURCE_ID=ALIAS:NAME [--config PATH]",
  "alias-add": "keyper alias-add NAME --provider local|convex|vercel|structured|ssh|ssh-structured|wrangler [provider options] [--config PATH]",
  "issuer-add": "keyper issuer-add NAME --provider cloudflare-access-service-token --account-id ALIAS:NAME --api-token ALIAS:NAME --token-name NAME [--duration DURATION] [--config PATH]",
  plan: "keyper plan copy|copy-many|sync|move|remove|generate|compose|put|enable|disable|issue ...",
  doctor: "keyper doctor [--json] [--config PATH]",
  "cloudflare-doctor": "keyper cloudflare-doctor ISSUER --zone DOMAIN [--json] [--config PATH]",
  twilio: "keyper twilio list profiles|accounts|trunks|numbers|routing [--profile NAME]\nkeyper twilio use PROFILE\nkeyper twilio import PROFILE --to PATH/.env.local [--overwrite]",
  livekit: "keyper livekit list projects|trunks|numbers|routing [--project NAME]\nkeyper livekit use PROJECT\nkeyper livekit import PROJECT --to PATH/.env.local [--overwrite]",
  "install-rules": "keyper install-rules (--project PATH | --global)"
};

interface ParsedArgs {
  positionals: string[];
  options: Map<string, Array<string | true>>;
  pairs: Map<string, [string, string]>;
}

const BOOLEAN_OPTIONS = new Set([
  "--all", "--json", "--overwrite", "--sensitive", "--stdin",
  "--accept-unverified-source-deletion", "--global"
]);
const MAX_STDIN_BYTES = 16 * 1024 * 1024;

function usageError(): never {
  process.stderr.write(`${USAGE}\n`);
  process.exitCode = 2;
  throw new Error("usage");
}

function parseArgs(args: string[]): ParsedArgs {
  const positionals: string[] = [];
  const options = new Map<string, Array<string | true>>();
  const pairs = new Map<string, [string, string]>();
  for (let index = 0; index < args.length; index += 1) {
    const value = args[index] ?? "";
    if (!value.startsWith("--")) {
      positionals.push(value);
      continue;
    }
    if (BOOLEAN_OPTIONS.has(value)) {
      options.set(value, [...(options.get(value) ?? []), true]);
      continue;
    }
    if (value === "--between") {
      const left = args[index + 1];
      const right = args[index + 2];
      if (!left || !right || left.startsWith("--") || right.startsWith("--") || pairs.has(value)) usageError();
      pairs.set(value, [left, right]);
      index += 2;
      continue;
    }
    const next = args[index + 1];
    if (!next || next.startsWith("--")) usageError();
    options.set(value, [...(options.get(value) ?? []), next]);
    index += 1;
  }
  return { positionals, options, pairs };
}

function optionValues(parsed: ParsedArgs, name: string): string[] {
  const values = parsed.options.get(name) ?? [];
  if (values.some((value) => typeof value !== "string")) usageError();
  return values as string[];
}

function option(parsed: ParsedArgs, name: string): string | undefined {
  const values = optionValues(parsed, name);
  if (values.length > 1) usageError();
  return values[0];
}

function flag(parsed: ParsedArgs, name: string): boolean {
  const values = parsed.options.get(name) ?? [];
  if (values.length > 1 || values.some((value) => value !== true)) usageError();
  return values.length === 1;
}

function assertOptions(parsed: ParsedArgs, allowed: readonly string[], allowedPairs: readonly string[] = []): void {
  const optionSet = new Set(allowed);
  const pairSet = new Set(allowedPairs);
  if (
    [...parsed.options].some(([name]) => !optionSet.has(name)) ||
    [...parsed.pairs].some(([name]) => !pairSet.has(name))
  ) usageError();
}

function reference(value: string): { alias: string; name: string } {
  const separator = value.indexOf(":");
  if (separator < 1 || separator === value.length - 1) usageError();
  return { alias: value.slice(0, separator), name: value.slice(separator + 1) };
}

function targets(parsed: ParsedArgs, defaultName: string): Destination[] {
  const values = optionValues(parsed, "--to");
  if (values.length === 0) usageError();
  const sensitive = flag(parsed, "--sensitive");
  return values.map((value) => {
    const separator = value.indexOf(":");
    return {
      alias: separator === -1 ? value : value.slice(0, separator),
      name: separator === -1 ? defaultName : value.slice(separator + 1),
      sensitive
    };
  });
}

function fields(parsed: ParsedArgs): Record<string, string> {
  const result: Record<string, string> = {};
  for (const value of optionValues(parsed, "--field")) {
    const separator = value.indexOf("=");
    if (separator < 1 || separator === value.length - 1) usageError();
    const name = requireName(value.slice(0, separator));
    if (result[name]) usageError();
    result[name] = value.slice(separator + 1);
  }
  return result;
}

function issueTargets(parsed: ParsedArgs): IssueDestination[] {
  const outputs = new Set<IssuerOutput>(["CLIENT_ID", "CLIENT_SECRET", "RESOURCE_ID"]);
  return optionValues(parsed, "--to").map((value) => {
    const separator = value.indexOf("=");
    if (separator < 1 || separator === value.length - 1) usageError();
    const output = value.slice(0, separator) as IssuerOutput;
    if (!outputs.has(output)) usageError();
    return { output, ...reference(value.slice(separator + 1)) };
  });
}

function printJson(value: unknown): void {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

function printPlan(
  config: KeyperConfig,
  operation: string,
  sources: Array<{ alias: string; name: string }>,
  destinations: Destination[]
): void {
  for (const source of sources) {
    requireAlias(config, source.alias);
    requireName(source.name);
  }
  for (const destination of destinations) {
    const alias = requireAlias(config, destination.alias);
    requireName(destination.name ?? "");
    if (destination.sensitive && alias.provider !== "vercel") throw new KeyperError("CONFIG_INVALID");
  }
  for (const source of sources) process.stdout.write(`${operation}-source\t${source.alias}\t${source.name}\n`);
  for (const destination of destinations) {
    process.stdout.write(`${operation}-destination\t${destination.alias}\t${destination.name ?? ""}\n`);
  }
}

function printFanout(operation: string, result: FanoutResult): void {
  for (const destination of result.destinations) {
    process.stdout.write(`${operation}\t${destination.alias}\t${destination.name}\t${destination.status}\n`);
  }
  if (!result.ok) {
    process.stderr.write("error FANOUT_PARTIAL: Not every destination was updated; supported writes were rolled back.\n");
    process.exitCode = 5;
  }
}

async function readStdin(): Promise<Buffer> {
  if (process.stdin.isTTY) throw new KeyperError("STDIN_REQUIRED");
  const chunks: Buffer[] = [];
  let size = 0;
  try {
    for await (const chunk of process.stdin) {
      const bytes = Buffer.isBuffer(chunk) ? Buffer.from(chunk) : Buffer.from(chunk as string);
      size += bytes.length;
      if (size > MAX_STDIN_BYTES) {
        bytes.fill(0);
        throw new KeyperError("INPUT_TOO_LARGE");
      }
      chunks.push(bytes);
    }
    return Buffer.concat(chunks);
  } finally {
    for (const chunk of chunks) chunk.fill(0);
  }
}

async function run(): Promise<void> {
  let [command, ...rest] = process.argv.slice(2);
  if (!command || command === "--help" || command === "-h") {
    process.stdout.write(`${USAGE}\n`);
    return;
  }
  let planning = false;
  if (command === "plan") {
    if (rest.length === 1 && (rest[0] === "--help" || rest[0] === "-h")) {
      process.stdout.write(`${HELP.plan}\n`);
      return;
    }
    planning = true;
    command = rest.shift();
    if (!command) usageError();
  }
  if (rest.includes("--help") || rest.includes("-h")) {
    const help = HELP[command];
    if (!help) usageError();
    process.stdout.write(`${help}\n`);
    return;
  }

  const parsed = parseArgs(rest);
  if (command === "install-rules") {
    if (planning) usageError();
    assertOptions(parsed, ["--global", "--project"]);
    const global = flag(parsed, "--global");
    const project = option(parsed, "--project");
    if (global === Boolean(project) || parsed.positionals.length > 0) usageError();
    await installGuardrails({ global, ...(project ? { project } : {}) });
    process.stdout.write(`installed\t${global ? "global" : "project"}\n`);
    return;
  }

  if (command === "twilio" || command === "livekit") {
    if (planning) usageError();
    const [action, subject] = parsed.positionals;
    const listSubjects = command === "twilio"
      ? new Set(["profiles", "accounts", "trunks", "numbers", "routing"])
      : new Set(["projects", "trunks", "numbers", "routing"]);
    if (action === "list" && subject && listSubjects.has(subject) && parsed.positionals.length === 2) {
      assertOptions(parsed, [command === "twilio" ? "--profile" : "--project"]);
      const selected = option(parsed, command === "twilio" ? "--profile" : "--project");
      const result = command === "twilio"
        ? await listTwilio(subject as Parameters<typeof listTwilio>[0], selected)
        : await listLiveKit(subject as Parameters<typeof listLiveKit>[0], selected);
      printJson(result);
      return;
    }
    if (action === "use" && subject && parsed.positionals.length === 2) {
      assertOptions(parsed, []);
      if (command === "twilio") await useTwilio(subject);
      else await useLiveKit(subject);
      process.stdout.write(`${command}\t${subject}\tactive\n`);
      return;
    }
    if (action === "import" && subject && parsed.positionals.length === 2) {
      assertOptions(parsed, ["--to", "--overwrite"]);
      const path = option(parsed, "--to");
      if (!path) usageError();
      if (command === "twilio") await importTwilio(subject, path, flag(parsed, "--overwrite"));
      else await importLiveKit(subject, path, flag(parsed, "--overwrite"));
      process.stdout.write(`${command}\t${subject}\t${path}\timported\n`);
      return;
    }
    usageError();
  }

  if (command === "alias-add") {
    if (planning) usageError();
    assertOptions(parsed, ["--provider", "--path", "--host", "--format", "--field", "--deployment", "--cwd", "--project", "--environment", "--scope", "--git-branch", "--config"]);
    const name = parsed.positionals[0];
    const provider = option(parsed, "--provider");
    if (!name || parsed.positionals.length !== 1 || !provider) usageError();
    const path = option(parsed, "--path");
    const host = option(parsed, "--host");
    const cwd = option(parsed, "--cwd");
    const scope = option(parsed, "--scope");
    const gitBranch = option(parsed, "--git-branch");
    let alias: AliasConfig;
    if (provider === "local" && path) alias = { provider, path };
    else if (provider === "convex" && option(parsed, "--deployment")) {
      alias = { provider, deployment: option(parsed, "--deployment")!, ...(cwd ? { cwd } : {}) };
    } else if (provider === "vercel" && option(parsed, "--project") && option(parsed, "--environment")) {
      alias = {
        provider,
        project: option(parsed, "--project")!,
        environment: option(parsed, "--environment")!,
        ...(scope ? { scope } : {}),
        ...(gitBranch ? { gitBranch } : {})
      };
    } else if (provider === "structured" && path) alias = { provider, path, fields: fields(parsed) };
    else if (provider === "ssh" && path && host && ["dotenv", "systemd"].includes(option(parsed, "--format") ?? "")) {
      alias = { provider, path, host, format: option(parsed, "--format") as "dotenv" | "systemd" };
    } else if (provider === "ssh-structured" && path && host) alias = { provider, path, host, fields: fields(parsed) };
    else if (provider === "wrangler") alias = { provider };
    else usageError();
    await addAlias(option(parsed, "--config"), name, alias);
    process.stdout.write(`alias-add\t${name}\t${provider}\tadded\n`);
    return;
  }

  if (command === "issuer-add") {
    if (planning) usageError();
    assertOptions(parsed, ["--provider", "--account-id", "--api-token", "--token-name", "--duration", "--config"]);
    const name = parsed.positionals[0];
    const provider = option(parsed, "--provider");
    if (!name || parsed.positionals.length !== 1 || provider !== "cloudflare-access-service-token") usageError();
    const accountId = option(parsed, "--account-id");
    const apiToken = option(parsed, "--api-token");
    const tokenName = option(parsed, "--token-name");
    const duration = option(parsed, "--duration");
    if (!accountId || !apiToken || !tokenName) usageError();
    const issuer: IssuerConfig = {
      provider,
      accountId: reference(accountId),
      apiToken: reference(apiToken),
      tokenName,
      ...(duration ? { duration } : {})
    };
    await addIssuer(option(parsed, "--config"), name, issuer);
    process.stdout.write(`issuer-add\t${name}\t${provider}\tadded\n`);
    return;
  }

  const loaded = await loadConfig(option(parsed, "--config"));
  const keyper = new Keyper(loaded.config);

  if (command === "list") {
    if (planning) usageError();
    assertOptions(parsed, ["--source", "--all", "--json", "--config"]);
    if (parsed.positionals.length > 0) usageError();
    const source = option(parsed, "--source");
    if (source && flag(parsed, "--all")) usageError();
    const inventory = source ? { [source]: await keyper.inventory(source) } : await keyper.inventoryAll();
    if (flag(parsed, "--json")) printJson(inventory);
    else for (const [alias, entries] of Object.entries(inventory)) {
      if (entries === "failed") process.stdout.write(`${alias}\tfailed\n`);
      else for (const entry of entries) process.stdout.write(`${alias}\t${entry.name}\t${entry.portability}\n`);
    }
    if (Object.values(inventory).includes("failed")) process.exitCode = 5;
    return;
  }

  if (command === "copy" || command === "move") {
    assertOptions(parsed, command === "copy"
      ? ["--from", "--to", "--to-name", "--overwrite", "--sensitive", "--config"]
      : ["--from", "--to", "--to-name", "--overwrite", "--sensitive", "--confirm-delete-source", "--accept-unverified-source-deletion", "--config"]);
    const name = parsed.positionals[0];
    const from = option(parsed, "--from");
    const to = option(parsed, "--to");
    if (!name || parsed.positionals.length !== 1 || !from || !to) usageError();
    const toName = option(parsed, "--to-name") ?? name;
    if (planning) {
      printPlan(loaded.config, command, [{ alias: from, name }], [{ alias: to, name: toName, sensitive: flag(parsed, "--sensitive") }]);
      return;
    }
    const request = { name, from, to, toName, overwrite: flag(parsed, "--overwrite"), sensitive: flag(parsed, "--sensitive") };
    const result = command === "copy"
      ? await keyper.copy(request)
      : await keyper.move({
          ...request,
          confirmDeleteSource: option(parsed, "--confirm-delete-source") ?? "",
          acceptUnverifiedSourceDeletion: flag(parsed, "--accept-unverified-source-deletion")
        });
    process.stdout.write(`${command}\t${from}\t${name}\t${to}\t${result.destinationName}\t${result.verified ? "verified" : "unverified"}\n`);
    return;
  }

  if (command === "remove") {
    assertOptions(parsed, ["--from", "--confirm-delete", "--config"]);
    const name = parsed.positionals[0];
    const from = option(parsed, "--from");
    if (!name || parsed.positionals.length !== 1 || !from) usageError();
    if (planning) {
      printPlan(loaded.config, command, [{ alias: from, name }], []);
      return;
    }
    await keyper.remove(name, from, option(parsed, "--confirm-delete") ?? "");
    process.stdout.write(`remove\t${from}\t${name}\tdeleted\n`);
    return;
  }

  if (command === "copy-many") {
    assertOptions(parsed, ["--from", "--to", "--overwrite", "--sensitive", "--config"]);
    const name = parsed.positionals[0];
    const from = option(parsed, "--from");
    if (!name || parsed.positionals.length !== 1 || !from) usageError();
    const destinations = targets(parsed, name);
    if (planning) {
      printPlan(loaded.config, command, [{ alias: from, name }], destinations);
      return;
    }
    printFanout(command, await keyper.copyMany({ name, from, destinations, overwrite: flag(parsed, "--overwrite") }));
    return;
  }

  if (command === "compare") {
    if (planning) usageError();
    assertOptions(parsed, ["--config"], ["--between"]);
    let left: { alias: string; name: string };
    let right: { alias: string; name: string };
    const between = parsed.pairs.get("--between");
    if (between && parsed.positionals.length === 1) {
      left = { alias: between[0], name: parsed.positionals[0]! };
      right = { alias: between[1], name: parsed.positionals[0]! };
    } else if (!between && parsed.positionals.length === 2) {
      left = reference(parsed.positionals[0]!);
      right = reference(parsed.positionals[1]!);
    } else usageError();
    const result = await keyper.compare(left.name, left.alias, right.alias, right.name);
    process.stdout.write(`compare\t${left.alias}\t${left.name}\t${right.alias}\t${right.name}\t${result}\n`);
    if (result !== "equal") process.exitCode = 4;
    return;
  }

  if (command === "audit") {
    if (planning) usageError();
    assertOptions(parsed, ["--all", "--json", "--config"]);
    const check = parsed.positionals[0];
    const all = flag(parsed, "--all");
    if (parsed.positionals.length > 1 || all === Boolean(check)) usageError();
    const configured = loaded.config.checks ?? {};
    const names = all ? Object.keys(configured).sort() : [check!];
    if (names.length === 0 || names.some((name) => !Object.hasOwn(configured, name))) throw new KeyperError("CHECK_NOT_FOUND");
    const result: Record<string, Awaited<ReturnType<Keyper["audit"]>>> = {};
    for (const name of names) result[name] = await keyper.audit(configured[name]!);
    if (flag(parsed, "--json")) printJson(result);
    else for (const [name, status] of Object.entries(result)) process.stdout.write(`audit\t${name}\t${status}\n`);
    const statuses = Object.values(result);
    if (statuses.includes("failed")) process.exitCode = 5;
    else if (statuses.some((status) => status !== "equal")) process.exitCode = 4;
    return;
  }

  if (command === "sync") {
    assertOptions(parsed, ["--from", "--overwrite", "--config"]);
    const check = parsed.positionals[0];
    const from = option(parsed, "--from");
    if (!check || parsed.positionals.length !== 1 || !from) usageError();
    const configured = loaded.config.checks ?? {};
    if (!Object.hasOwn(configured, check)) throw new KeyperError("CHECK_NOT_FOUND");
    const source = reference(from);
    requireAlias(loaded.config, source.alias);
    requireName(source.name);
    const references = configured[check]!;
    if (!references.some((candidate) => candidate.alias === source.alias && candidate.name === source.name)) {
      throw new KeyperError("CHECK_SOURCE_REQUIRED");
    }
    const destinations = references
      .filter((candidate) => candidate.alias !== source.alias || candidate.name !== source.name)
      .map(({ alias, name }) => ({ alias, name }));
    if (planning) {
      printPlan(loaded.config, command, [source], destinations);
      return;
    }
    printFanout(command, await keyper.copyMany({
      name: source.name,
      from: source.alias,
      destinations,
      overwrite: flag(parsed, "--overwrite")
    }));
    return;
  }

  if (["generate", "put", "enable", "disable"].includes(command)) {
    const allowed = command === "generate"
      ? ["--to", "--bytes", "--overwrite", "--sensitive", "--config"]
      : command === "put"
        ? ["--to", "--stdin", "--overwrite", "--sensitive", "--config"]
        : ["--to", "--overwrite", "--config"];
    assertOptions(parsed, allowed);
    const name = parsed.positionals[0];
    if (!name || parsed.positionals.length !== 1) usageError();
    if (command === "put" && !flag(parsed, "--stdin")) usageError();
    const destinations = targets(parsed, name);
    if (planning) {
      printPlan(loaded.config, command, [], destinations);
      return;
    }
    if (command === "generate") {
      const bytesText = option(parsed, "--bytes");
      const bytes = bytesText === undefined ? 32 : Number(bytesText);
      printFanout(command, await keyper.generate(name, destinations, flag(parsed, "--overwrite"), bytes));
      return;
    }
    const value = command === "put" ? await readStdin() : Buffer.from(command === "enable" ? "true" : "false");
    try {
      printFanout(command, await keyper.put(
        name,
        value,
        destinations,
        flag(parsed, "--overwrite"),
        command === "enable" || command === "disable" ? "boolean" : "string"
      ));
    } finally {
      value.fill(0);
    }
    return;
  }

  if (command === "compose") {
    assertOptions(parsed, ["--from", "--to", "--format", "--overwrite", "--sensitive", "--config"]);
    const name = parsed.positionals[0];
    const format = option(parsed, "--format");
    if (!name || parsed.positionals.length !== 1 || (format !== "csv" && format !== "json")) usageError();
    const sources = optionValues(parsed, "--from").map(reference);
    if (sources.length === 0) usageError();
    const destinations = targets(parsed, name);
    if (planning) {
      printPlan(loaded.config, command, sources, destinations);
      return;
    }
    printFanout(command, await keyper.compose(name, sources, format, destinations, flag(parsed, "--overwrite")));
    return;
  }

  if (command === "issue") {
    assertOptions(parsed, ["--to", "--config"]);
    const issuer = parsed.positionals[0];
    if (!issuer || parsed.positionals.length !== 1) usageError();
    const destinations = issueTargets(parsed);
    if (planning) {
      printPlan(loaded.config, command, [], destinations);
      return;
    }
    printFanout(command, await keyper.issue(issuer, destinations));
    return;
  }

  if (command === "doctor") {
    if (planning) usageError();
    assertOptions(parsed, ["--json", "--config"]);
    if (parsed.positionals.length > 0) usageError();
    const result = await keyper.doctor();
    if (flag(parsed, "--json")) printJson(result);
    else for (const [alias, status] of Object.entries(result)) process.stdout.write(`${alias}\t${status}\n`);
    if (Object.values(result).includes("failed")) process.exitCode = 5;
    return;
  }

  if (command === "cloudflare-doctor") {
    if (planning) usageError();
    assertOptions(parsed, ["--zone", "--json", "--config"]);
    const issuer = parsed.positionals[0];
    const zone = option(parsed, "--zone");
    if (!issuer || parsed.positionals.length !== 1 || !zone) usageError();
    const result = await keyper.cloudflareDoctor(issuer, zone);
    if (flag(parsed, "--json")) printJson(result);
    else for (const [capability, available] of Object.entries(result)) {
      process.stdout.write(`${capability}\t${available ? "yes" : "no"}\n`);
    }
    if (!result.tunnelAccess || !result.accessApps || !result.accessServiceTokens) process.exitCode = 5;
    return;
  }

  usageError();
}

run().catch((error: unknown) => {
  if (process.exitCode === 2) return;
  const safe = error instanceof KeyperError ? error : asSafeError(error);
  process.stderr.write(`error ${safe.code}: ${safe.message}\n`);
  process.exitCode = 5;
});
