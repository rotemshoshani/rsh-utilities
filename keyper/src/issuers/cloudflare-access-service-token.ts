import { KeyperError } from "../errors.js";
import type { CloudflareAccessServiceTokenIssuer } from "../types.js";

type Fetch = typeof fetch;

interface IssuedServiceToken {
  resourceId: Buffer;
  clientId: Buffer;
  clientSecret: Buffer;
  revoke(): Promise<void>;
}

export interface CloudflareProbeResult {
  tunnelAccess: boolean;
  accessApps: boolean;
  accessServiceTokens: boolean;
  zoneReadable: boolean;
  zonePresent: boolean;
  zoneActive: boolean;
}

async function probeRequest(url: string, apiToken: Buffer, fetchImpl: Fetch): Promise<{ ok: boolean; result?: unknown }> {
  const response = await fetchImpl(url, {
    headers: { authorization: `Bearer ${apiToken.toString("utf8")}` },
    redirect: "error"
  });
  const bytes = Buffer.from(await response.arrayBuffer());
  try {
    if (!response.ok) return { ok: false };
    const payload = JSON.parse(bytes.toString("utf8")) as { success?: unknown; result?: unknown };
    return payload.success === true ? { ok: true, result: payload.result } : { ok: false };
  } catch {
    throw new KeyperError("PROVIDER_FAILED");
  } finally {
    bytes.fill(0);
  }
}

export async function probeCloudflare(
  accountId: Buffer,
  apiToken: Buffer,
  zoneName: string,
  fetchImpl: Fetch = fetch
): Promise<CloudflareProbeResult> {
  const account = accountId.toString("utf8");
  if (!/^[a-f0-9]{32}$/i.test(account) || !/^[a-z0-9.-]+$/i.test(zoneName)) throw new KeyperError("CONFIG_INVALID");
  const base = "https://api.cloudflare.com/client/v4";
  const [tunnels, apps, serviceTokens, zones] = await Promise.all([
    probeRequest(`${base}/accounts/${account}/cfd_tunnel?per_page=1`, apiToken, fetchImpl),
    probeRequest(`${base}/accounts/${account}/access/apps?per_page=1`, apiToken, fetchImpl),
    probeRequest(`${base}/accounts/${account}/access/service_tokens?per_page=1`, apiToken, fetchImpl),
    probeRequest(`${base}/zones?name=${encodeURIComponent(zoneName)}&account.id=${account}&per_page=1`, apiToken, fetchImpl)
  ]);
  const zone = Array.isArray(zones.result) ? zones.result[0] : undefined;
  return {
    tunnelAccess: tunnels.ok,
    accessApps: apps.ok,
    accessServiceTokens: serviceTokens.ok,
    zoneReadable: zones.ok,
    zonePresent: Boolean(zone),
    zoneActive: Boolean(zone && typeof zone === "object" && (zone as { status?: unknown }).status === "active")
  };
}

async function request(
  config: CloudflareAccessServiceTokenIssuer,
  accountId: Buffer,
  apiToken: Buffer,
  path: string,
  init: RequestInit,
  fetchImpl: Fetch
): Promise<unknown> {
  const account = accountId.toString("utf8");
  if (!/^[a-f0-9]{32}$/i.test(account)) throw new KeyperError("CONFIG_INVALID");
  const response = await fetchImpl(`https://api.cloudflare.com/client/v4/accounts/${account}${path}`, {
    ...init,
    headers: {
      authorization: `Bearer ${apiToken.toString("utf8")}`,
      "content-type": "application/json"
    },
    redirect: "error"
  });
  const bytes = Buffer.from(await response.arrayBuffer());
  try {
    if (!response.ok) throw new KeyperError(response.status === 401 || response.status === 403 ? "PROVIDER_AUTH" : "PROVIDER_FAILED");
    const payload = JSON.parse(bytes.toString("utf8")) as { success?: unknown; result?: unknown };
    if (payload.success !== true) throw new KeyperError("PROVIDER_FAILED");
    return payload.result;
  } catch (error) {
    if (error instanceof KeyperError) throw error;
    throw new KeyperError("PROVIDER_FAILED");
  } finally {
    bytes.fill(0);
  }
}

export async function issueCloudflareAccessServiceToken(
  config: CloudflareAccessServiceTokenIssuer,
  accountId: Buffer,
  apiToken: Buffer,
  fetchImpl: Fetch = fetch
): Promise<IssuedServiceToken> {
  const existing = await request(
    config,
    accountId,
    apiToken,
    `/access/service_tokens?name=${encodeURIComponent(config.tokenName)}`,
    { method: "GET" },
    fetchImpl
  );
  if (!Array.isArray(existing)) throw new KeyperError("PROVIDER_FAILED");
  if (existing.some((entry) => entry && typeof entry === "object" && (entry as { name?: unknown }).name === config.tokenName)) {
    throw new KeyperError("ISSUER_EXISTS");
  }

  const created = await request(
    config,
    accountId,
    apiToken,
    "/access/service_tokens",
    {
      method: "POST",
      body: JSON.stringify({
        name: config.tokenName,
        ...(config.duration ? { duration: config.duration } : {})
      })
    },
    fetchImpl
  );
  if (!created || typeof created !== "object") throw new KeyperError("PROVIDER_FAILED");
  const value = created as { id?: unknown; client_id?: unknown; client_secret?: unknown };
  if (typeof value.id !== "string" || typeof value.client_id !== "string" || typeof value.client_secret !== "string") {
    throw new KeyperError("PROVIDER_FAILED");
  }
  const resourceId = Buffer.from(value.id);
  const clientId = Buffer.from(value.client_id);
  const clientSecret = Buffer.from(value.client_secret);
  return {
    resourceId,
    clientId,
    clientSecret,
    async revoke() {
      await request(
        config,
        accountId,
        apiToken,
        `/access/service_tokens/${encodeURIComponent(resourceId.toString("utf8"))}`,
        { method: "DELETE" },
        fetchImpl
      );
    }
  };
}
