import { spawn } from "node:child_process";
import { constants } from "node:os";
import { basename, isAbsolute, resolve } from "node:path";
import { KeyperError } from "./errors.js";
import type { KeyperConfig, ToolConfig } from "./types.js";

export const REDACTED = "[redacted]";

// Commands whose purpose is to print environment, file, or process contents. A declared tool
// must be a program that uses a secret, never one that can echo it back.
const VALUE_DUMPERS = new Set([
  "env", "printenv", "set", "export", "declare", "typeset", "compgen", "echo", "printf",
  "cat", "tac", "nl", "sed", "awk", "gawk", "mawk", "nawk", "grep", "egrep", "fgrep", "rg", "ag", "ack",
  "head", "tail", "less", "more", "most", "view", "vi", "vim", "nvim", "nano", "emacs",
  "strings", "xxd", "od", "hexdump", "hd", "base64", "base32", "basenc", "cut", "sort", "uniq",
  "tee", "dd", "cp", "mv", "install", "diff", "cmp", "paste", "join", "column", "jq", "yq", "dotenv",
  "xargs", "sudo", "doas", "su", "nohup", "time", "nice", "ionice", "timeout", "stdbuf", "setsid",
  "strace", "ltrace", "gdb", "lldb", "script", "watch", "exec", "eval", "source", "ps", "pgrep", "procs",
  "top", "htop", "docker", "podman", "kubectl"
]);
const SHELLS = new Set([
  "sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh", "busybox", "toybox",
  "pwsh", "powershell", "cmd", "nu", "xonsh", "elvish", "rc"
]);
const INLINE_CODE_FLAGS: ReadonlyArray<[RegExp, ReadonlySet<string>]> = [
  [/^(python|pypy)[0-9.]*$/, new Set(["-c"])],
  [/^(node|nodejs)$/, new Set(["-e", "--eval", "-p", "--print"])],
  [/^(bun)$/, new Set(["-e", "--eval", "-p", "--print"])],
  [/^(deno)$/, new Set(["eval", "repl"])],
  [/^(perl)[0-9.]*$/, new Set(["-e", "-E"])],
  [/^(ruby)$/, new Set(["-e"])],
  [/^(php)$/, new Set(["-r", "-a"])],
  [/^(lua|luajit)$/, new Set(["-e", "-i"])],
  [/^(rscript)$/, new Set(["-e"])],
  [/^(osascript)$/, new Set(["-e"])],
  [/^(tclsh|wish)$/, new Set([])]
];
const ENV_FILE_NAME = /^(\.env(\..*)?|.*\.env|\.dev\.vars|\.envrc|environ)$/i;

function programName(command: string): string {
  return basename(command).toLowerCase().replace(/\.(exe|cmd|bat)$/, "");
}

function argumentPaths(argument: string): string[] {
  const separator = argument.indexOf("=");
  return separator > 0 ? [argument, argument.slice(separator + 1)] : [argument];
}

function secretFilePaths(config: KeyperConfig): Set<string> {
  const paths = new Set<string>();
  for (const alias of Object.values(config.aliases)) {
    if (alias.provider === "local" || alias.provider === "structured") paths.add(resolve(alias.path));
  }
  return paths;
}

function refersToSecretFile(argument: string, cwd: string, secretFiles: Set<string>): boolean {
  for (const candidate of argumentPaths(argument)) {
    if (!candidate) continue;
    if (ENV_FILE_NAME.test(basename(candidate))) return true;
    if (/(^|\/)proc\/[^/]+\/environ$/.test(candidate)) return true;
    if (secretFiles.has(isAbsolute(candidate) ? resolve(candidate) : resolve(cwd, candidate))) return true;
  }
  return false;
}

// Deliberate non-printing check. Output scrubbing is the second line of defence; this refuses
// tools whose only plausible effect is to reveal what was injected.
export function assertSafeTool(config: KeyperConfig, tool: ToolConfig, passthrough: readonly string[] = []): void {
  const [command, ...declared] = tool.command;
  const program = programName(command ?? "");
  if (!program || VALUE_DUMPERS.has(program)) throw new KeyperError("TOOL_UNSAFE", `Refused command: ${program || "(empty)"}.`);
  if (SHELLS.has(program)) throw new KeyperError("TOOL_UNSAFE", `Refused shell: ${program}.`);
  const interpreter = INLINE_CODE_FLAGS.find(([pattern]) => pattern.test(program));
  if (interpreter) {
    // Interpreters must run a declared script; inline code or a bare REPL could print the environment.
    const scriptIndex = declared.findIndex((argument) => !argument.startsWith("-"));
    const options = scriptIndex === -1 ? declared : declared.slice(0, scriptIndex);
    if (scriptIndex === -1 || interpreter[1].has(declared[scriptIndex]!.toLowerCase())) {
      throw new KeyperError("TOOL_UNSAFE", `Refused ${program} without a declared script.`);
    }
    if (options.some((option) => interpreter[1].has(option) || [...interpreter[1]].some((flag) => flag.startsWith("-") && option.startsWith(flag)))) {
      throw new KeyperError("TOOL_UNSAFE", `Refused ${program} inline code.`);
    }
  }
  const secretFiles = secretFilePaths(config);
  if ([...declared, ...passthrough].some((argument) => refersToSecretFile(argument, tool.cwd, secretFiles))) {
    throw new KeyperError("TOOL_UNSAFE", "Refused an argument that refers to an environment or secret file.");
  }
}

function base64Stems(value: Buffer, alphabet: "base64" | "base64url"): string[] {
  // A value embedded in a larger base64 payload (for example "user:KEY" in Basic auth) encodes
  // differently at each of the three byte alignments; keep only characters it fully determines.
  const stems: string[] = [];
  for (let offset = 0; offset < 3; offset += 1) {
    const shifted = Buffer.concat([Buffer.alloc(offset), value]);
    try {
      const encoded = shifted.toString(alphabet).replace(/=+$/, "");
      const start = Math.ceil((offset * 8) / 6);
      const end = Math.floor(((offset + value.length) * 8) / 6);
      if (end - start >= 8) stems.push(encoded.slice(start, end));
    } finally {
      shifted.fill(0);
    }
  }
  return stems;
}

function textForms(text: string): string[] {
  const percent = encodeURIComponent(text);
  return [
    text,
    JSON.stringify(text).slice(1, -1),
    percent,
    percent.replace(/%[0-9A-F]{2}/g, (match) => match.toLowerCase()),
    percent.replace(/%20/g, "+"),
    encodeURI(text)
  ];
}

// Every representation of a value that Keyper redacts from tool output.
export function secretForms(value: Buffer): string[] {
  const text = value.toString("utf8");
  const bases = new Set([text, text.trim(), text.replace(/\r?\n/g, "\r\n"), text.replace(/\r\n/g, "\n")]);
  const lines = text.split(/\r?\n/).map((line) => line.trim());
  const forms = new Set<string>();
  for (const base of bases) {
    if (!base) continue;
    for (const form of textForms(base)) forms.add(form);
    const bytes = Buffer.from(base, "utf8");
    try {
      for (const alphabet of ["base64", "base64url"] as const) {
        forms.add(bytes.toString(alphabet));
        forms.add(bytes.toString(alphabet).replace(/=+$/, ""));
        for (const stem of base64Stems(bytes, alphabet)) forms.add(stem);
      }
      forms.add(bytes.toString("hex"));
      forms.add(bytes.toString("hex").toUpperCase());
    } finally {
      bytes.fill(0);
    }
  }
  if (lines.length > 1) for (const line of lines) if (line.length >= 8) forms.add(line);
  forms.delete("");
  return [...forms].sort((left, right) => right.length - left.length);
}

// Masks every (including overlapping) occurrence of every form, then collapses masked runs.
export function scrub(text: string, forms: readonly string[]): { text: string; redactions: number } {
  const mask = new Uint8Array(text.length);
  for (const form of forms) {
    for (let index = text.indexOf(form); index !== -1; index = text.indexOf(form, index + 1)) {
      mask.fill(1, index, index + form.length);
    }
  }
  let output = "";
  let redactions = 0;
  for (let index = 0; index < text.length;) {
    if (!mask[index]) {
      const next = mask.indexOf(1, index);
      const end = next === -1 ? text.length : next;
      output += text.slice(index, end);
      index = end;
      continue;
    }
    while (index < text.length && mask[index]) index += 1;
    output += REDACTED;
    redactions += 1;
  }
  return { text: output, redactions };
}

const decoder = new TextDecoder("utf-8", { fatal: true });

export function scrubOutput(output: Buffer, forms: readonly string[]): { text: string; redactions: number } {
  let text: string;
  try {
    text = decoder.decode(output);
  } catch {
    throw new KeyperError("OUTPUT_UNSAFE");
  }
  if (text.includes("\0")) throw new KeyperError("OUTPUT_UNSAFE");
  return scrub(text, forms);
}

export function environmentValue(value: Buffer): string {
  let text: string;
  try {
    text = decoder.decode(value);
  } catch {
    throw new KeyperError("VALUE_UNSUPPORTED");
  }
  if (!text || text.includes("\0")) throw new KeyperError("VALUE_UNSUPPORTED");
  return text;
}

export interface ToolRunResult {
  exitCode: number;
  stdout: string;
  stderr: string;
  redactions: number;
}

export interface ToolProcess {
  command: readonly string[];
  args: readonly string[];
  cwd: string;
  env: Readonly<Record<string, string>>;
  secrets: readonly Buffer[];
  timeoutMs: number;
  maxOutputBytes: number;
}

// Runs without a shell; values reach the child only through its environment block. Output is
// buffered in full so redaction never sees a value split across chunks, and nothing is
// released unless every stream decodes as text and is scrubbed.
export async function runTool(request: ToolProcess): Promise<ToolRunResult> {
  const forms = [...new Set(request.secrets.flatMap(secretForms))].sort((left, right) => right.length - left.length);
  const [command, ...declared] = request.command;
  if (!command) throw new KeyperError("TOOL_UNSAFE");
  const raw = await new Promise<{ code: number; stdout: Buffer; stderr: Buffer }>((resolveRun, reject) => {
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let total = 0;
    let settled = false;
    const discard = () => {
      for (const chunk of [...stdout, ...stderr]) chunk.fill(0);
    };
    const child = spawn(command, [...declared, ...request.args], {
      cwd: request.cwd,
      env: { ...process.env, ...request.env },
      shell: false,
      windowsHide: true,
      detached: true,
      stdio: ["ignore", "pipe", "pipe"]
    });
    const kill = () => {
      try {
        if (child.pid) process.kill(-child.pid, "SIGKILL");
      } catch {
        child.kill("SIGKILL");
      }
    };
    const fail = (error: KeyperError) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      kill();
      discard();
      reject(error);
    };
    const collect = (target: Buffer[]) => (chunk: Buffer) => {
      if (settled) {
        chunk.fill(0);
        return;
      }
      total += chunk.length;
      if (total > request.maxOutputBytes) {
        chunk.fill(0);
        fail(new KeyperError("OUTPUT_TOO_LARGE"));
        return;
      }
      target.push(Buffer.from(chunk));
      chunk.fill(0);
    };
    const timer = setTimeout(() => fail(new KeyperError("TOOL_TIMEOUT")), request.timeoutMs);
    child.on("error", () => fail(new KeyperError("TOOL_UNAVAILABLE")));
    child.stdout.on("data", collect(stdout));
    child.stderr.on("data", collect(stderr));
    child.on("close", (code, signal) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const exitCode = code ?? 128 + (signal ? constants.signals[signal] ?? 0 : 0);
      const result = { code: exitCode, stdout: Buffer.concat(stdout), stderr: Buffer.concat(stderr) };
      discard();
      resolveRun(result);
    });
  });
  try {
    const stdout = scrubOutput(raw.stdout, forms);
    const stderr = scrubOutput(raw.stderr, forms);
    return { exitCode: raw.code, stdout: stdout.text, stderr: stderr.text, redactions: stdout.redactions + stderr.redactions };
  } finally {
    raw.stdout.fill(0);
    raw.stderr.fill(0);
  }
}
