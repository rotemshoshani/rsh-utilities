import { spawn } from "node:child_process";
import { basename, dirname } from "node:path";

export type FileGitStatus = "ignored" | "tracked" | "untracked-not-ignored" | "not-in-repo" | "unknown";

// Exit status only: git output (paths) is discarded so nothing from the file is ever read.
async function git(cwd: string, args: readonly string[]): Promise<number> {
  return await new Promise((resolve) => {
    const child = spawn("git", ["-C", cwd, ...args], { shell: false, stdio: "ignore", windowsHide: true });
    const timer = setTimeout(() => child.kill("SIGKILL"), 10_000);
    child.on("error", () => {
      clearTimeout(timer);
      resolve(-1);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve(code ?? -1);
    });
  });
}

export async function fileGitStatus(path: string): Promise<FileGitStatus> {
  const directory = dirname(path);
  const name = basename(path);
  const inside = await git(directory, ["rev-parse", "--is-inside-work-tree"]);
  if (inside === -1) return "unknown";
  if (inside !== 0) return "not-in-repo";
  const tracked = await git(directory, ["ls-files", "--error-unmatch", "--", name]);
  if (tracked === 0) return "tracked";
  const ignored = await git(directory, ["check-ignore", "-q", "--", name]);
  if (ignored === 0) return "ignored";
  if (ignored === 1) return "untracked-not-ignored";
  return "unknown";
}

export function isGitWarning(status: FileGitStatus): boolean {
  return status === "tracked" || status === "untracked-not-ignored";
}
