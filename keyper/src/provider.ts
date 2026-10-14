import type { AliasConfig, KeyProvider } from "./types.js";
import { ConvexProvider } from "./providers/convex.js";
import { LocalProvider } from "./providers/local.js";
import { VercelProvider } from "./providers/vercel.js";
import { StructuredProvider } from "./providers/structured.js";
import { SshProvider } from "./providers/ssh.js";
import { SshStructuredProvider } from "./providers/ssh-structured.js";
import { WranglerProvider } from "./providers/wrangler.js";

export function createProvider(config: AliasConfig): KeyProvider {
  switch (config.provider) {
    case "local":
      return new LocalProvider(config.path);
    case "convex":
      return new ConvexProvider(config.deployment, config.cwd);
    case "vercel":
      return new VercelProvider(config);
    case "structured":
      return new StructuredProvider(config.path, config.fields);
    case "ssh":
      return new SshProvider(config);
    case "ssh-structured":
      return new SshStructuredProvider(config);
    case "wrangler":
      return new WranglerProvider();
  }
}
