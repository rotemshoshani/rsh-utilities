import { describe, expect, it, vi } from "vitest";

vi.mock("../src/safe-process.js", () => ({
  runSafeProcess: vi.fn(async () => Buffer.from(JSON.stringify({ token: "x".repeat(32), type: "oauth" })))
}));

describe("Wrangler provider", () => {
  it("exposes the authenticated token by name without writing it", async () => {
    const { WranglerProvider } = await import("../src/providers/wrangler.js");
    const provider = new WranglerProvider();
    expect(await provider.list()).toEqual([{ name: "CLOUDFLARE_API_TOKEN", portability: "portable" }]);
    const value = await provider.read("CLOUDFLARE_API_TOKEN");
    expect(value.byteLength).toBe(32);
    value.fill(0);
  });
});
