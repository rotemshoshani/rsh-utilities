import { KeyperError } from "../errors.js";
import { runSafeProcess } from "../safe-process.js";
import type { InventoryEntry, KeyProvider, WriteOptions } from "../types.js";

const TOKEN_NAME = "CLOUDFLARE_API_TOKEN";

async function readToken(): Promise<Buffer> {
  const output = await runSafeProcess(
    "npx",
    ["--yes", "wrangler@latest", "auth", "token", "--json"]
  );
  try {
    const payload = JSON.parse(output.toString("utf8")) as { token?: unknown };
    if (typeof payload.token !== "string" || payload.token.length < 16) throw new KeyperError("PROVIDER_FAILED");
    return Buffer.from(payload.token, "utf8");
  } catch (error) {
    if (error instanceof KeyperError) throw error;
    throw new KeyperError("PROVIDER_FAILED");
  } finally {
    output.fill(0);
  }
}

export class WranglerProvider implements KeyProvider {
  readonly kind = "wrangler" as const;

  async list(): Promise<InventoryEntry[]> {
    const token = await readToken();
    token.fill(0);
    return [{ name: TOKEN_NAME, portability: "portable" }];
  }

  async read(name: string): Promise<Buffer> {
    if (name !== TOKEN_NAME) throw new KeyperError("SOURCE_MISSING");
    return await readToken();
  }

  async write(_name: string, _value: Buffer, _options: WriteOptions): Promise<{ verifiable: boolean }> {
    throw new KeyperError("PROVIDER_FAILED");
  }

  async remove(): Promise<void> {
    throw new KeyperError("PROVIDER_FAILED");
  }

  async doctor(): Promise<void> {
    const token = await readToken();
    token.fill(0);
  }
}
