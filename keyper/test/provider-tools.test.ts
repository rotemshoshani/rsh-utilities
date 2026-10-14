import { randomBytes, timingSafeEqual } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { importLiveKit, importTwilio, listLiveKit, listTwilio } from "../src/provider-tools.js";
import { LocalProvider } from "../src/providers/local.js";

const temporaryDirectories: string[] = [];

afterEach(async () => {
  while (temporaryDirectories.length > 0) await rm(temporaryDirectories.pop()!, { recursive: true, force: true });
});

async function directory(): Promise<string> {
  const path = await mkdtemp(join(tmpdir(), "keyper-provider-tools-"));
  temporaryDirectories.push(path);
  return path;
}

function equal(value: Buffer, expected: string): boolean {
  const bytes = Buffer.from(expected);
  try {
    return value.length === bytes.length && timingSafeEqual(value, bytes);
  } finally {
    bytes.fill(0);
  }
}

describe("provider tools", () => {
  it("accepts null or missing Twilio region/edge and ignores unused top-level settings", async () => {
    const root = await directory();
    const apiKey = randomBytes(32).toString("hex");
    const apiSecret = randomBytes(32).toString("hex");
    const config = join(root, "config.json");
    await writeFile(config, JSON.stringify({
      activeProject: "production",
      futureHarmlessSetting: { enabled: true },
      profiles: {
        production: { accountSid: "AC123", region: null, apiKey, apiSecret, futurePrivateSetting: apiSecret },
        secondary: { accountSid: "AC456", edge: null, apiKey, apiSecret }
      }
    }), { mode: 0o600 });
    const result = JSON.stringify(await listTwilio("profiles", undefined, undefined, config));
    expect(result).toContain("AC123");
    expect(result).toContain("AC456");
    expect(result).not.toContain("region");
    expect(result).not.toContain("edge");
    expect(result).not.toContain("futureHarmlessSetting");
    expect(result).not.toContain(apiKey);
    expect(result).not.toContain(apiSecret);
  });

  it("rejects an unknown Twilio output column before returning any row", async () => {
    const credential = randomBytes(32).toString("hex");
    const runner = async () => Buffer.from(`sid\tstatus\ttype\tauthToken\nAC123\tactive\tFull\t${credential}\n`);
    await expect(listTwilio("accounts", undefined, runner)).rejects.toMatchObject({ code: "PROVIDER_SCHEMA" });
  });

  it("never requests or returns Twilio webhook URLs", async () => {
    const canary = randomBytes(32).toString("hex");
    let message = "";
    try {
      await listTwilio("routing", undefined, async (_command, args) => {
        expect(args.join(",")).not.toContain("voiceUrl");
        expect(args.join(",")).not.toContain("smsUrl");
        return Buffer.from(`sid\tphoneNumber\ttrunkSid\tvoiceMethod\tsmsMethod\tvoiceUrl\nPN1\t+100\tTK1\tPOST\tPOST\thttps://example.invalid/?key=${canary}\n`);
      });
    } catch (error: unknown) {
      message = error instanceof Error ? error.message : "";
    }
    expect(message).toBe("The provider returned an unrecognized or unsafe response schema.");
    expect(message).not.toContain(canary);
  });

  it("removes LiveKit project and SIP credentials by schema", async () => {
    const apiKey = randomBytes(32).toString("hex");
    const apiSecret = randomBytes(32).toString("hex");
    const projects = async () => Buffer.from(JSON.stringify([
      { Name: "production", ProjectId: "project-1", URL: "wss://example.invalid", APIKey: apiKey, APISecret: apiSecret }
    ]));
    const safeProjects = JSON.stringify(await listLiveKit("projects", undefined, projects));
    expect(safeProjects).not.toContain(apiKey);
    expect(safeProjects).not.toContain(apiSecret);

    const authPassword = randomBytes(32).toString("hex");
    const metadata = randomBytes(32).toString("hex");
    const responses = [
      { items: [{ sipTrunkId: "ST-in", name: "inbound", numbers: ["+100"], authUsername: "user", authPassword, metadata }] },
      { items: [{ sipTrunkId: "ST-out", name: "outbound", address: "sip.example.invalid", numbers: ["+100"], authUsername: "user", authPassword, metadata }] }
    ];
    const trunks = JSON.stringify(await listLiveKit("trunks", undefined, async () => Buffer.from(JSON.stringify(responses.shift()))));
    expect(trunks).not.toContain(authPassword);
    expect(trunks).not.toContain(metadata);
    expect(trunks).not.toContain("authUsername");
  });

  it("fails closed on unknown LiveKit fields", async () => {
    const credential = randomBytes(32).toString("hex");
    const runner = async () => Buffer.from(JSON.stringify([
      { Name: "production", ProjectId: "project-1", URL: "wss://example.invalid", APIKey: "key", APISecret: "secret", authExport: credential }
    ]));
    await expect(listLiveKit("projects", undefined, runner)).rejects.toMatchObject({ code: "PROVIDER_SCHEMA" });
  });

  it("returns LiveKit dispatch agent names but suppresses room and agent metadata", async () => {
    const roomMetadata = randomBytes(32).toString("hex");
    const agentMetadata = randomBytes(32).toString("hex");
    const runner = async () => Buffer.from(JSON.stringify({ items: [{
      sipDispatchRuleId: "SDR1",
      roomConfig: {
        name: "private-room-config",
        metadata: roomMetadata,
        agents: [{ agentName: "verification-agent", metadata: agentMetadata, deployment: "private-deployment" }]
      }
    }] }));
    const result = JSON.stringify(await listLiveKit("routing", undefined, runner));
    expect(result).toContain("verification-agent");
    expect(result).not.toContain(roomMetadata);
    expect(result).not.toContain(agentMetadata);
    expect(result).not.toContain("private-room-config");
    expect(result).not.toContain("private-deployment");
  });

  it("imports both providers atomically into .env.local without returning values", async () => {
    const root = await directory();
    const target = join(root, ".env.local");
    const twilioConfig = join(root, "twilio.json");
    const twilioSecret = randomBytes(32).toString("hex");
    const liveKitSecret = randomBytes(32).toString("hex");
    await writeFile(twilioConfig, JSON.stringify({
      profiles: { production: { accountSid: "AC123", apiKey: "SK123", apiSecret: twilioSecret } }
    }), { mode: 0o600 });
    expect(await importTwilio("production", target, false, twilioConfig)).toBeUndefined();
    const liveKitRunner = async () => Buffer.from(JSON.stringify([
      { Name: "production", ProjectId: "project-1", URL: "wss://example.invalid", APIKey: "LK123", APISecret: liveKitSecret }
    ]));
    expect(await importLiveKit("production", target, false, liveKitRunner)).toBeUndefined();

    const provider = new LocalProvider(target);
    const twilio = await provider.read("TWILIO_API_SECRET");
    const liveKit = await provider.read("LIVEKIT_API_SECRET");
    try {
      expect(equal(twilio, twilioSecret)).toBe(true);
      expect(equal(liveKit, liveKitSecret)).toBe(true);
    } finally {
      twilio.fill(0);
      liveKit.fill(0);
    }
  });
});
