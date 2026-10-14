import { randomBytes } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Keyper } from "../src/core.js";
import { KeyperError } from "../src/errors.js";
import type { AliasConfig, InventoryEntry, KeyProvider, WriteOptions } from "../src/types.js";

class FakeProvider implements KeyProvider {
  readonly kind = "local" as const;
  readonly values = new Map<string, Buffer>();
  readonly failWrites = new Set<string>();
  async list(): Promise<InventoryEntry[]> { return [...this.values.keys()].map((name) => ({ name, portability: "portable" })); }
  async read(name: string): Promise<Buffer> {
    const value = this.values.get(name);
    if (!value) throw new KeyperError("SOURCE_MISSING");
    return Buffer.from(value);
  }
  async write(name: string, value: Buffer, options: WriteOptions): Promise<{ verifiable: boolean }> {
    if (this.failWrites.has(name)) throw new KeyperError("PROVIDER_FAILED");
    if (this.values.has(name) && !options.overwrite) throw new KeyperError("DESTINATION_EXISTS");
    this.values.set(name, Buffer.from(value));
    return { verifiable: true };
  }
  async remove(name: string): Promise<void> { this.values.delete(name); }
  async doctor(): Promise<void> {}
}

function response(result: unknown) {
  return new Response(JSON.stringify({ success: true, result }), { status: 200 });
}

afterEach(() => vi.unstubAllGlobals());

describe("provider-issued credentials", () => {
  it("fans out every one-time output and revokes on destination failure", async () => {
    const sourceConfig: AliasConfig = { provider: "local", path: "/source" };
    const destinationConfig: AliasConfig = { provider: "local", path: "/destination" };
    const source = new FakeProvider();
    const destination = new FakeProvider();
    source.values.set("ACCOUNT", Buffer.from(randomBytes(16).toString("hex")));
    source.values.set("API_TOKEN", Buffer.from(randomBytes(48).toString("base64url")));
    destination.failWrites.add("CF_SECRET");
    const providers = new Map<AliasConfig, FakeProvider>([[sourceConfig, source], [destinationConfig, destination]]);
    const resourceId = "11111111-2222-3333-4444-555555555555";
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response({ id: resourceId, client_id: "client", client_secret: "secret" }))
      .mockResolvedValueOnce(response({ id: resourceId }));
    vi.stubGlobal("fetch", fetchMock);
    const keyper = new Keyper({
      version: 1,
      aliases: { source: sourceConfig, destination: destinationConfig },
      issuers: {
        cloudflare: {
          provider: "cloudflare-access-service-token",
          accountId: { alias: "source", name: "ACCOUNT" },
          apiToken: { alias: "source", name: "API_TOKEN" },
          tokenName: "test-token"
        }
      }
    }, (config) => providers.get(config)!);
    const result = await keyper.issue("cloudflare", [
      { output: "CLIENT_ID", alias: "destination", name: "CF_ID" },
      { output: "CLIENT_SECRET", alias: "destination", name: "CF_SECRET" },
      { output: "RESOURCE_ID", alias: "destination", name: "CF_RESOURCE" }
    ]);
    expect(result.ok).toBe(false);
    expect(destination.values.size).toBe(0);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
