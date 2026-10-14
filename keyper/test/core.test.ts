import { randomBytes, timingSafeEqual } from "node:crypto";
import { describe, expect, it } from "vitest";
import { Keyper } from "../src/core.js";
import { KeyperError } from "../src/errors.js";
import type { AliasConfig, InventoryEntry, KeyProvider, WriteOptions } from "../src/types.js";

class FakeProvider implements KeyProvider {
  readonly values = new Map<string, { value: Buffer; locked: boolean }>();
  readonly failWrites = new Set<string>();
  readonly corruptReads = new Set<string>();
  readonly corruptAfterWrite = new Set<string>();
  readonly readBuffers: Buffer[] = [];
  failList = false;

  constructor(readonly kind: AliasConfig["provider"]) {}

  async list(): Promise<InventoryEntry[]> {
    if (this.failList) throw new KeyperError("PROVIDER_FAILED");
    return [...this.values].map(([name, value]) => ({
      name,
      portability: value.locked ? "locked" : "portable"
    }));
  }

  async read(name: string): Promise<Buffer> {
    const entry = this.values.get(name);
    if (!entry) throw new KeyperError("SOURCE_MISSING");
    if (entry.locked) throw new KeyperError("VALUE_LOCKED");
    const corrupt = this.corruptReads.delete(name);
    const result = corrupt ? Buffer.concat([entry.value, Buffer.of(0)]) : Buffer.from(entry.value);
    this.readBuffers.push(result);
    return result;
  }

  async write(name: string, value: Buffer, options: WriteOptions): Promise<{ verifiable: boolean }> {
    if (this.failWrites.has(name)) throw new KeyperError("PROVIDER_FAILED");
    if (this.values.has(name) && !options.overwrite) throw new KeyperError("DESTINATION_EXISTS");
    this.values.set(name, { value: Buffer.from(value), locked: options.sensitive });
    if (this.corruptAfterWrite.delete(name)) this.corruptReads.add(name);
    return { verifiable: !options.sensitive };
  }

  async remove(name: string): Promise<void> {
    if (!this.values.delete(name)) throw new KeyperError("SOURCE_MISSING");
  }

  async doctor(): Promise<void> {}
}

function fixture(): {
  keyper: Keyper;
  source: FakeProvider;
  destination: FakeProvider;
} {
  const sourceConfig: AliasConfig = { provider: "local", path: "/unused-a" };
  const destinationConfig: AliasConfig = {
    provider: "vercel",
    project: "test",
    environment: "preview"
  };
  const source = new FakeProvider("local");
  const destination = new FakeProvider("vercel");
  const providers = new Map<AliasConfig, FakeProvider>([
    [sourceConfig, source],
    [destinationConfig, destination]
  ]);
  const keyper = new Keyper(
    { version: 1, aliases: { source: sourceConfig, destination: destinationConfig } },
    (config) => providers.get(config)!
  );
  return { keyper, source, destination };
}

function buffersEqual(left: Buffer, right: Buffer): boolean {
  return left.length === right.length && timingSafeEqual(left, right);
}

describe("Keyper", () => {
  it("copies and verifies a portable value", async () => {
    const { keyper, source, destination } = fixture();
    const value = randomBytes(48);
    source.values.set("API_KEY", { value: Buffer.from(value), locked: false });
    const result = await keyper.copy({
      name: "API_KEY",
      from: "source",
      to: "destination",
      overwrite: false,
      sensitive: false
    });
    expect(result.verified).toBe(true);
    expect(buffersEqual(destination.values.get("API_KEY")!.value, value)).toBe(true);
    value.fill(0);
  });

  it("deletes a source only after verified move", async () => {
    const { keyper, source } = fixture();
    const value = randomBytes(48);
    source.values.set("API_KEY", { value, locked: false });
    await keyper.move({
      name: "API_KEY",
      from: "source",
      to: "destination",
      overwrite: false,
      sensitive: false,
      confirmDeleteSource: "API_KEY",
      acceptUnverifiedSourceDeletion: false
    });
    expect(source.values.has("API_KEY")).toBe(false);
  });

  it("requires exact confirmation before removing a value", async () => {
    const { keyper, source } = fixture();
    source.values.set("API_KEY", { value: randomBytes(48), locked: false });
    await expect(keyper.remove("API_KEY", "source", "OTHER")).rejects.toMatchObject({ code: "CONFIRMATION_REQUIRED" });
    expect(source.values.has("API_KEY")).toBe(true);
    await keyper.remove("API_KEY", "source", "API_KEY");
    expect(source.values.has("API_KEY")).toBe(false);
  });

  it("requires acknowledgement before writing a sensitive move", async () => {
    const { keyper, source, destination } = fixture();
    source.values.set("API_KEY", { value: randomBytes(48), locked: false });
    await expect(keyper.move({
      name: "API_KEY",
      from: "source",
      to: "destination",
      overwrite: false,
      sensitive: true,
      confirmDeleteSource: "API_KEY",
      acceptUnverifiedSourceDeletion: false
    })).rejects.toMatchObject({ code: "UNVERIFIED_DELETE_REQUIRES_ACK" });
    expect(destination.values.has("API_KEY")).toBe(false);
    expect(source.values.has("API_KEY")).toBe(true);
  });

  it("refuses to read a locked source", async () => {
    const { keyper, source } = fixture();
    source.values.set("API_KEY", { value: randomBytes(48), locked: true });
    await expect(keyper.copy({
      name: "API_KEY",
      from: "source",
      to: "destination",
      overwrite: false,
      sensitive: false
    })).rejects.toMatchObject({ code: "VALUE_LOCKED" });
  });

  it("refuses a same-name copy to the same resolved location", async () => {
    const alias: AliasConfig = { provider: "local", path: "/same" };
    const provider = new FakeProvider("local");
    provider.values.set("API_KEY", { value: randomBytes(48), locked: false });
    const keyper = new Keyper(
      { version: 1, aliases: { first: alias, second: { ...alias } } },
      () => provider
    );
    await expect(keyper.copy({
      name: "API_KEY",
      from: "first",
      to: "second",
      overwrite: true,
      sensitive: false
    })).rejects.toMatchObject({ code: "SAME_LOCATION" });
    expect(provider.values.has("API_KEY")).toBe(true);
  });

  it("compares different names without exposing either value", async () => {
    const { keyper, source, destination } = fixture();
    const value = randomBytes(48);
    source.values.set("FIRST", { value: Buffer.from(value), locked: false });
    destination.values.set("SECOND", { value: Buffer.from(value), locked: false });
    expect(await keyper.compare("FIRST", "source", "destination", "SECOND")).toBe("equal");
    value.fill(0);
  });

  it("audits reusable reference groups without retaining values", async () => {
    const { keyper, source, destination } = fixture();
    const references = [{ alias: "source", name: "TOKEN" }, { alias: "destination", name: "TOKEN" }];
    const value = randomBytes(48);
    source.values.set("TOKEN", { value: Buffer.from(value), locked: false });
    destination.values.set("TOKEN", { value: Buffer.from(value), locked: false });
    expect(await keyper.audit(references)).toBe("equal");
    expect([...source.readBuffers, ...destination.readBuffers].every((buffer) => buffer.every((byte) => byte === 0))).toBe(true);
    destination.values.set("TOKEN", { value: randomBytes(48), locked: false });
    expect(await keyper.audit(references)).toBe("different");
    destination.values.delete("TOKEN");
    expect(await keyper.audit(references)).toBe("missing");
    destination.values.set("TOKEN", { value: randomBytes(48), locked: true });
    expect(await keyper.audit(references)).toBe("locked");
    destination.failList = true;
    expect(await keyper.audit(references)).toBe("failed");
    value.fill(0);
  });

  it("rolls back completed fan-out writes after a later failure", async () => {
    const sourceConfig: AliasConfig = { provider: "local", path: "/source" };
    const firstConfig: AliasConfig = { provider: "local", path: "/first" };
    const secondConfig: AliasConfig = { provider: "local", path: "/second" };
    const source = new FakeProvider("local");
    const first = new FakeProvider("local");
    const second = new FakeProvider("local");
    const value = randomBytes(48);
    source.values.set("TOKEN", { value: Buffer.from(value), locked: false });
    second.failWrites.add("TOKEN");
    const providers = new Map<AliasConfig, FakeProvider>([
      [sourceConfig, source], [firstConfig, first], [secondConfig, second]
    ]);
    const keyper = new Keyper(
      { version: 1, aliases: { source: sourceConfig, first: firstConfig, second: secondConfig } },
      (config) => providers.get(config)!
    );
    const result = await keyper.copyMany({
      name: "TOKEN",
      from: "source",
      destinations: [{ alias: "first" }, { alias: "second" }],
      overwrite: false
    });
    expect(result).toEqual({
      ok: false,
      destinations: [
        { alias: "first", name: "TOKEN", status: "rolled-back" },
        { alias: "second", name: "TOKEN", status: "failed" }
      ]
    });
    expect(first.values.has("TOKEN")).toBe(false);
    value.fill(0);
  });

  it("returns successful inventories alongside sanitized failures", async () => {
    const goodConfig: AliasConfig = { provider: "local", path: "/good" };
    const badConfig: AliasConfig = { provider: "local", path: "/bad" };
    const good = new FakeProvider("local");
    const bad = new FakeProvider("local");
    good.values.set("PRESENT", { value: randomBytes(32), locked: false });
    bad.list = async () => { throw new Error("provider output must not escape"); };
    const providers = new Map<AliasConfig, FakeProvider>([[goodConfig, good], [badConfig, bad]]);
    const keyper = new Keyper(
      { version: 1, aliases: { good: goodConfig, bad: badConfig } },
      (config) => providers.get(config)!
    );
    expect(await keyper.inventoryAll()).toEqual({
      bad: "failed",
      good: [{ name: "PRESENT", portability: "portable" }]
    });
  });

  it("restores an overwritten value when copy verification fails", async () => {
    const { keyper, source, destination } = fixture();
    const original = randomBytes(48);
    source.values.set("TOKEN", { value: randomBytes(48), locked: false });
    destination.values.set("TOKEN", { value: Buffer.from(original), locked: false });
    destination.corruptAfterWrite.add("TOKEN");

    await expect(keyper.copy({
      name: "TOKEN",
      from: "source",
      to: "destination",
      overwrite: true,
      sensitive: false
    })).rejects.toMatchObject({ code: "VALUES_DIFFER" });

    expect(buffersEqual(destination.values.get("TOKEN")!.value, original)).toBe(true);
    original.fill(0);
  });

  it("removes a newly created value when copy verification fails", async () => {
    const { keyper, source, destination } = fixture();
    source.values.set("TOKEN", { value: randomBytes(48), locked: false });
    destination.corruptAfterWrite.add("TOKEN");

    await expect(keyper.copy({
      name: "TOKEN",
      from: "source",
      to: "destination",
      overwrite: false,
      sensitive: false
    })).rejects.toMatchObject({ code: "VALUES_DIFFER" });

    expect(destination.values.has("TOKEN")).toBe(false);
  });

  it("rolls back a write that fails internal verification", async () => {
    const { keyper, source, destination } = fixture();
    source.values.set("TOKEN", { value: randomBytes(48), locked: false });
    destination.corruptReads.add("TOKEN");
    const result = await keyper.copyMany({
      name: "TOKEN",
      from: "source",
      destinations: [{ alias: "destination" }],
      overwrite: false
    });
    expect(result).toEqual({
      ok: false,
      destinations: [{ alias: "destination", name: "TOKEN", status: "failed" }]
    });
    expect(destination.values.has("TOKEN")).toBe(false);
  });
});
