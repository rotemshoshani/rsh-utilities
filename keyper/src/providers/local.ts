import { randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { chmod, lstat, mkdir, open, readFile, rename, unlink } from "node:fs/promises";
import { dirname } from "node:path";
import { parse } from "dotenv";
import { KeyperError } from "../errors.js";
import type { InventoryEntry, KeyProvider, WriteOptions } from "../types.js";

interface RecordRange {
  name: string;
  start: number;
  end: number;
}

function assignmentRanges(lines: string[]): RecordRange[] {
  const records: RecordRange[] = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] ?? "";
    const match = line.match(/^\s*(?:export\s+)?([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!match?.[1]) continue;
    const remainder = match[2] ?? "";
    const quote = remainder[0] === "'" || remainder[0] === '"' || remainder[0] === "`"
      ? remainder[0]
      : undefined;
    let end = index;
    if (quote) {
      let fragment = remainder.slice(1);
      while (!hasClosingQuote(fragment, quote) && end + 1 < lines.length) {
        end += 1;
        fragment += `\n${lines[end] ?? ""}`;
      }
      if (!hasClosingQuote(fragment, quote)) throw new KeyperError("LOCAL_FILE_INVALID");
    }
    records.push({ name: match[1], start: index, end });
    index = end;
  }
  const seen = new Set<string>();
  for (const record of records) {
    if (seen.has(record.name)) throw new KeyperError("LOCAL_FILE_INVALID");
    seen.add(record.name);
  }
  return records;
}

function hasClosingQuote(value: string, quote: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    if (value[index] !== quote) continue;
    let backslashes = 0;
    for (let cursor = index - 1; cursor >= 0 && value[cursor] === "\\"; cursor -= 1) backslashes += 1;
    if (backslashes % 2 === 0) return true;
  }
  return false;
}

function serializeValue(value: string): string {
  const candidates = [
    ...(value.includes("\n") || value.includes("\r") ? [] : [value]),
    ...(value.includes("'") ? [] : [`'${value}'`]),
    ...(value.includes('"') ? [] : [`"${value}"`]),
    ...(value.includes("`") ? [] : [`\`${value}\``])
  ];
  for (const candidate of candidates) {
    if (parse(`KEYPER_VALUE=${candidate}`).KEYPER_VALUE === value) return candidate;
  }
  throw new KeyperError("LOCAL_FILE_INVALID");
}

async function safeRead(path: string): Promise<{ content: string; mode: number; exists: boolean }> {
  try {
    const stat = await lstat(path);
    if (stat.isSymbolicLink() || !stat.isFile()) throw new KeyperError("LOCAL_FILE_UNSAFE");
    const raw = await readFile(path);
    try {
      return { content: raw.toString("utf8"), mode: stat.mode & 0o600, exists: true };
    } finally {
      raw.fill(0);
    }
  } catch (error: unknown) {
    if (error instanceof KeyperError) throw error;
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return { content: "", mode: 0o600, exists: false };
    }
    throw new KeyperError("LOCAL_FILE_UNSAFE");
  }
}

async function atomicWrite(path: string, content: string, mode: number): Promise<void> {
  const parent = dirname(path);
  await mkdir(parent, { recursive: true, mode: 0o700 });
  const temporaryPath = `${path}.keyper-${randomUUID()}`;
  const bytes = Buffer.from(content, "utf8");
  let handle: Awaited<ReturnType<typeof open>> | undefined;
  try {
    handle = await open(temporaryPath, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, mode);
    await handle.writeFile(bytes);
    await handle.sync();
    await handle.close();
    handle = undefined;
    await chmod(temporaryPath, mode);
    await rename(temporaryPath, path);
  } catch {
    if (handle) await handle.close().catch(() => undefined);
    await unlink(temporaryPath).catch(() => undefined);
    throw new KeyperError("LOCAL_FILE_UNSAFE");
  } finally {
    bytes.fill(0);
  }
}

export class LocalProvider implements KeyProvider {
  readonly kind = "local" as const;

  constructor(private readonly path: string) {}

  async list(): Promise<InventoryEntry[]> {
    const { content } = await safeRead(this.path);
    if (!content) return [];
    return assignmentRanges(content.split(/\r?\n/))
      .map(({ name }) => ({ name, portability: "portable" as const }))
      .sort((left, right) => left.name.localeCompare(right.name));
  }

  async read(name: string): Promise<Buffer> {
    const { content } = await safeRead(this.path);
    const records = assignmentRanges(content.split(/\r?\n/));
    if (!records.some((record) => record.name === name)) throw new KeyperError("SOURCE_MISSING");
    const values = parse(content);
    if (!(name in values)) throw new KeyperError("LOCAL_FILE_INVALID");
    return Buffer.from(values[name] ?? "", "utf8");
  }

  async write(name: string, value: Buffer, options: WriteOptions): Promise<{ verifiable: boolean }> {
    return await this.writeMany([[name, value]], options);
  }

  async writeMany(values: ReadonlyArray<readonly [string, Buffer]>, options: WriteOptions): Promise<{ verifiable: boolean }> {
    const state = await safeRead(this.path);
    const lines = state.content ? state.content.split(/\r?\n/) : [];
    const records = assignmentRanges(lines);
    const replacements = new Map(values.map(([name, value]) => [name, `${name}=${serializeValue(value.toString("utf8"))}`]));
    if (replacements.size !== values.length) throw new KeyperError("LOCAL_FILE_INVALID");
    const existing = records.filter((record) => replacements.has(record.name));
    if (existing.length > 0 && !options.overwrite) throw new KeyperError("DESTINATION_EXISTS");
    for (const record of existing.sort((left, right) => right.start - left.start)) {
      lines.splice(record.start, record.end - record.start + 1, replacements.get(record.name)!);
      replacements.delete(record.name);
    }
    for (const serialized of replacements.values()) {
      if (lines.length > 0 && lines.at(-1) === "") lines.splice(lines.length - 1, 0, serialized);
      else lines.push(serialized);
    }
    const output = `${lines.join("\n").replace(/\n*$/, "")}\n`;
    await atomicWrite(this.path, output, state.exists ? state.mode : 0o600);
    return { verifiable: true };
  }

  async remove(name: string): Promise<void> {
    const state = await safeRead(this.path);
    const lines = state.content.split(/\r?\n/);
    const record = assignmentRanges(lines).find((candidate) => candidate.name === name);
    if (!record) throw new KeyperError("SOURCE_MISSING");
    lines.splice(record.start, record.end - record.start + 1);
    const output = lines.join("\n").replace(/^\n+|\n+$/g, "");
    await atomicWrite(this.path, output ? `${output}\n` : "", state.mode);
  }

  async doctor(): Promise<void> {
    await safeRead(this.path);
  }
}
