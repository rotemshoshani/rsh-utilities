import { randomBytes, timingSafeEqual } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { issueCloudflareAccessServiceToken, probeCloudflare } from "../src/issuers/cloudflare-access-service-token.js";

function json(result: unknown, status = 200) {
  return new Response(JSON.stringify({ success: status < 400, result }), {
    status,
    headers: { "content-type": "application/json" }
  });
}

describe("Cloudflare Access service-token issuer", () => {
  it("reports only capability and zone booleans", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json([]))
      .mockResolvedValueOnce(json([]))
      .mockResolvedValueOnce(json([], 403))
      .mockResolvedValueOnce(json([{ status: "active" }]));
    const result = await probeCloudflare(
      Buffer.from(randomBytes(16).toString("hex")),
      randomBytes(48),
      "example.com",
      fetchMock
    );
    expect(result).toEqual({
      tunnelAccess: true,
      accessApps: true,
      accessServiceTokens: false,
      zoneReadable: true,
      zonePresent: true,
      zoneActive: true
    });
  });

  it("captures one-time outputs and supports sanitized rollback", async () => {
    const accountId = Buffer.from(randomBytes(16).toString("hex"));
    const apiToken = randomBytes(48);
    const clientId = randomBytes(32);
    const clientSecret = randomBytes(48);
    const resourceId = "11111111-2222-3333-4444-555555555555";
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json([]))
      .mockResolvedValueOnce(json({
        id: resourceId,
        client_id: clientId.toString("base64url"),
        client_secret: clientSecret.toString("base64url")
      }))
      .mockResolvedValueOnce(json({ id: resourceId }));
    const issued = await issueCloudflareAccessServiceToken({
      provider: "cloudflare-access-service-token",
      accountId: { alias: "source", name: "ACCOUNT" },
      apiToken: { alias: "source", name: "TOKEN" },
      tokenName: "test-token"
    }, accountId, apiToken, fetchMock);
    try {
      expect(timingSafeEqual(issued.clientId, Buffer.from(clientId.toString("base64url")))).toBe(true);
      expect(timingSafeEqual(issued.clientSecret, Buffer.from(clientSecret.toString("base64url")))).toBe(true);
      await issued.revoke();
      expect(fetchMock).toHaveBeenCalledTimes(3);
    } finally {
      accountId.fill(0); apiToken.fill(0); clientId.fill(0); clientSecret.fill(0);
      issued.clientId.fill(0); issued.clientSecret.fill(0); issued.resourceId.fill(0);
    }
  });

  it("never includes a provider response in its error", async () => {
    const canary = randomBytes(48);
    const fetchMock = vi.fn().mockResolvedValue(new Response(canary, { status: 403 }));
    let message = "";
    try {
      await issueCloudflareAccessServiceToken({
        provider: "cloudflare-access-service-token",
        accountId: { alias: "source", name: "ACCOUNT" },
        apiToken: { alias: "source", name: "TOKEN" },
        tokenName: "test-token"
      }, Buffer.from(randomBytes(16).toString("hex")), randomBytes(48), fetchMock);
    } catch (error: unknown) {
      message = error instanceof Error ? error.message : "";
    }
    expect(message).toBe("The provider rejected authorization for the requested operation.");
    expect(Buffer.from(message).includes(canary)).toBe(false);
    canary.fill(0);
  });
});
