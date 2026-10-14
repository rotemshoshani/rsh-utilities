import { timingSafeEqual } from "node:crypto";
import { describe, expect, it } from "vitest";
import { ConvexProvider } from "../src/providers/convex.js";
import type { SafeProcessOptions } from "../src/safe-process.js";

function equal(left: Buffer, right: Buffer): boolean {
  return left.length === right.length && timingSafeEqual(left, right);
}

function convexStub() {
  const values = new Map<string, Buffer>();
  const calls: Array<{ args: readonly string[]; input?: Buffer }> = [];
  const runner = async (
    _command: string,
    args: readonly string[],
    options: SafeProcessOptions = {}
  ): Promise<Buffer> => {
    calls.push({ args, ...(options.input ? { input: Buffer.from(options.input) } : {}) });
    const envIndex = args.indexOf("env");
    const operationIndex = envIndex + 3;
    const operation = args[operationIndex];
    if (operation === "list") {
      return Buffer.from([...values.keys()].join("\n") + (values.size ? "\n" : ""));
    }
    const name = args[operationIndex + 1]!;
    if (operation === "set") {
      const input = Buffer.from(options.input ?? Buffer.alloc(0));
      if (input.at(-1) === 0x0a) values.set(name, Buffer.from(input.subarray(0, -1)));
      else values.set(name, input);
      return Buffer.from("provider warning-shaped success output\n");
    }
    if (operation === "get") {
      const value = values.get(name);
      return value ? Buffer.concat([value, Buffer.of(0x0a)]) : Buffer.alloc(0);
    }
    if (operation === "remove") {
      values.delete(name);
      return Buffer.alloc(0);
    }
    throw new Error("unexpected synthetic provider operation");
  };
  return { values, calls, runner };
}

describe("ConvexProvider", () => {
  it("round-trips exact text through Convex's newline-trimming stdin transport", async () => {
    const stub = convexStub();
    const provider = new ConvexProvider("synthetic-deployment", undefined, stub.runner);
    const fixtures = [
      Buffer.alloc(0),
      Buffer.from("plain"),
      Buffer.from("trailing-newline\n"),
      Buffer.from("two-newlines\n\n"),
      Buffer.from("crlf\r\n"),
      Buffer.from("spaces, quotes ' \" and unicode 雪")
    ];

    for (const [index, value] of fixtures.entries()) {
      const name = `SYNTHETIC_${index}`;
      await provider.write(name, value, { overwrite: false, sensitive: false });
      const received = await provider.read(name);
      try {
        expect(equal(received, value)).toBe(true);
      } finally {
        received.fill(0);
        value.fill(0);
      }
    }

    const setCalls = stub.calls.filter(({ args }) => args.includes("set"));
    expect(setCalls).toHaveLength(fixtures.length);
    expect(setCalls.every(({ args }) => args.length === 6)).toBe(true);
    expect(setCalls.every(({ input }) => input?.at(-1) === 0x0a)).toBe(true);
    for (const call of stub.calls) call.input?.fill(0);
    for (const value of stub.values.values()) value.fill(0);
  });

  it("rejects usage or JSON output instead of treating it as inventory", async () => {
    for (const output of ["Usage: convex env\n", '{"NAME":"value"}\n']) {
      const provider = new ConvexProvider("synthetic-deployment", undefined, async () => Buffer.from(output));
      await expect(provider.list()).rejects.toMatchObject({ code: "PROVIDER_SCHEMA" });
    }
  });

  it("treats a missing name as missing before accepting silent get output", async () => {
    const provider = new ConvexProvider("synthetic-deployment", undefined, async () => Buffer.alloc(0));
    await expect(provider.read("ABSENT")).rejects.toMatchObject({ code: "SOURCE_MISSING" });
  });
});
