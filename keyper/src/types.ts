export type Portability = "portable" | "locked";

export interface LocalAlias {
  provider: "local";
  path: string;
}

export interface ConvexAlias {
  provider: "convex";
  deployment: string;
  cwd?: string;
}

export interface VercelAlias {
  provider: "vercel";
  project: string;
  scope?: string;
  environment: string;
  gitBranch?: string;
}

export interface StructuredAlias {
  provider: "structured";
  path: string;
  fields: Record<string, string>;
}

export interface SshAlias {
  provider: "ssh";
  host: string;
  path: string;
  format: "dotenv" | "systemd";
}

export interface SshStructuredAlias {
  provider: "ssh-structured";
  host: string;
  path: string;
  fields: Record<string, string>;
}

export interface WranglerAlias {
  provider: "wrangler";
}

export interface SecretReference {
  alias: string;
  name: string;
}

export interface CloudflareAccessServiceTokenIssuer {
  provider: "cloudflare-access-service-token";
  accountId: SecretReference;
  apiToken: SecretReference;
  tokenName: string;
  duration?: string;
}

export type IssuerConfig = CloudflareAccessServiceTokenIssuer;

export type AliasConfig = LocalAlias | ConvexAlias | VercelAlias | StructuredAlias | SshAlias | SshStructuredAlias | WranglerAlias;

export interface ToolConfig {
  command: string[];
  env: Record<string, SecretReference>;
  cwd: string;
  timeoutSeconds: number;
  maxOutputBytes: number;
}

export interface KeyperConfig {
  version: 1;
  aliases: Record<string, AliasConfig>;
  issuers?: Record<string, IssuerConfig>;
  checks?: Record<string, SecretReference[]>;
  tools?: Record<string, ToolConfig>;
}

export interface InventoryEntry {
  name: string;
  portability: Portability;
}

export interface WriteOptions {
  overwrite: boolean;
  sensitive: boolean;
  valueType?: "string" | "boolean" | "json";
}

export interface KeyProvider {
  readonly kind: AliasConfig["provider"];
  list(): Promise<InventoryEntry[]>;
  read(name: string): Promise<Buffer>;
  write(name: string, value: Buffer, options: WriteOptions): Promise<{ verifiable: boolean }>;
  remove(name: string): Promise<void>;
  doctor(): Promise<void>;
}

export interface LoadedConfig {
  config: KeyperConfig;
  configPath: string;
  baseDir: string;
}
