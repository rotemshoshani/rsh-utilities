import { randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { chmod, lstat, mkdir, open, readFile, rename, unlink } from "node:fs/promises";
import { dirname } from "node:path";
import { KeyperError } from "../errors.js";
import type { InventoryEntry, KeyProvider, WriteOptions } from "../types.js";

type JsonObject = Record<string, unknown>;

function segments(pointer: string): string[] {
  const result = pointer.slice(1).split("/").map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"));
  if (result.some((part) => part === "" || ["__proto__", "prototype", "constructor"].includes(part))) {
    throw new KeyperError("STRUCTURED_FILE_INVALID");
  }
  return result;
}

function locate(root: JsonObject, pointer: string): { parent: JsonObject; key: string; exists: boolean } {
  const path = segments(pointer);
  const key = path.pop();
  if (!key) throw new KeyperError("STRUCTURED_FILE_INVALID");
  let parent = root;
  for (const part of path) {
    const next = parent[part];
    if (!next || typeof next !== "object" || Array.isArray(next)) {
      throw new KeyperError("STRUCTURED_FILE_INVALID");
    }
    parent = next as JsonObject;
  }
  return { parent, key, exists: Object.prototype.hasOwnProperty.call(parent, key) };
}

async function load(path: string): Promise<JsonObject> {
  let bytes: Buffer | undefined;
  try {
    const stat = await lstat(path);
    if (stat.isSymbolicLink() || !stat.isFile()) throw new KeyperError("LOCAL_FILE_UNSAFE");
    bytes = await readFile(path);
    const parsed = JSON.parse(bytes.toString("utf8")) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new KeyperError("STRUCTURED_FILE_INVALID");
    }
    return parsed as JsonObject;
  } catch (error: unknown) {
    if (error instanceof KeyperError) throw error;
    throw new KeyperError("STRUCTURED_FILE_INVALID");
  } finally {
    bytes?.fill(0);
  }
}

async function atomicWrite(path: string, value: JsonObject): Promise<void> {
  const temporary = `${path}.keyper-${randomUUID()}`;
  const bytes = Buffer.from(`${JSON.stringify(value, null, 2)}\n`, "utf8");
  let handle: Awaited<ReturnType<typeof open>> | undefined;
  try {
    await mkdir(dirname(path), { recursive: true, mode: 0o700 });
    handle = await open(temporary, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY, 0o600);
    await handle.writeFile(bytes);
    await handle.sync();
    await handle.close();
    handle = undefined;
    await chmod(temporary, 0o600);
    await rename(temporary, path);
  } catch {
    if (handle) await handle.close().catch(() => undefined);
    await unlink(temporary).catch(() => undefined);
    throw new KeyperError("LOCAL_FILE_UNSAFE");
  } finally {
    bytes.fill(0);
  }
}

export class StructuredProvider implements KeyProvider {
  readonly kind = "structured" as const;

  constructor(
    private readonly path: string,
    private readonly fields: Record<string, string>
  ) {}

  async list(): Promise<InventoryEntry[]> {
    const root = await load(this.path);
    return Object.entries(this.fields)
      .filter(([, pointer]) => locate(root, pointer).exists)
      .map(([name]) => ({ name, portability: "portable" as const }))
      .sort((left, right) => left.name.localeCompare(right.name));
  }

  async read(name: string): Promise<Buffer> {
    const pointer = this.fields[name];
    if (!pointer) throw new KeyperError("NAME_INVALID");
    const root = await load(this.path);
    const field = locate(root, pointer);
    if (!field.exists) throw new KeyperError("SOURCE_MISSING");
    const value = field.parent[field.key];
    return Buffer.from(typeof value === "string" ? value : JSON.stringify(value), "utf8");
  }

  async write(name: string, value: Buffer, options: WriteOptions): Promise<{ verifiable: boolean }> {
    const pointer = this.fields[name];
    if (!pointer) throw new KeyperError("NAME_INVALID");
    const root = await load(this.path);
    const field = locate(root, pointer);
    if (field.exists && !options.overwrite) throw new KeyperError("DESTINATION_EXISTS");
    const text = value.toString("utf8");
    if (options.valueType === "boolean" && text !== "true" && text !== "false") {
      throw new KeyperError("STRUCTURED_FILE_INVALID");
    }
    field.parent[field.key] = options.valueType === "boolean"
      ? text === "true"
      : options.valueType === "json"
        ? JSON.parse(text) as unknown
        : text;
    await atomicWrite(this.path, root);
    return { verifiable: true };
  }

  async remove(name: string): Promise<void> {
    const pointer = this.fields[name];
    if (!pointer) throw new KeyperError("NAME_INVALID");
    const root = await load(this.path);
    const field = locate(root, pointer);
    if (!field.exists) throw new KeyperError("SOURCE_MISSING");
    delete field.parent[field.key];
    await atomicWrite(this.path, root);
  }

  async doctor(): Promise<void> {
    await load(this.path);
  }
}
