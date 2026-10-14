import { writeFileSync } from "node:fs";

export default function registerGlm53Flash(pi) {
  let lastRunFailed = false;

  pi.registerProvider("openrouter", {
    models: [
      {
        id: "z-ai/glm-5.3-flash",
        name: "Z.ai: GLM 5.3 Flash",
        reasoning: true,
        thinkingLevelMap: {
          off: null,
          minimal: null,
          low: "low",
          medium: null,
          high: "high",
          xhigh: null,
          max: "max",
        },
        input: ["text", "image"],
        cost: {
          input: 0.075,
          output: 0.25,
          cacheRead: 0.015,
          cacheWrite: 0,
        },
        contextWindow: 1310720,
        // OpenRouter preflights maximum response cost. The provider's 131k
        // ceiling can cause payment-required errors for ordinary coding turns.
        maxTokens: 16384,
        compat: {
          supportsDeveloperRole: false,
          thinkingFormat: "openrouter",
        },
      },
    ],
  });

  pi.on("agent_end", async (event) => {
    const lastAssistant = [...event.messages]
      .reverse()
      .find((message) => message?.role === "assistant");
    lastRunFailed = Boolean(
      lastAssistant &&
        (lastAssistant.stopReason === "error" ||
          lastAssistant.stopReason === "aborted" ||
          lastAssistant.errorMessage),
    );
  });

  pi.on("agent_settled", async (_event, ctx) => {
    const readyFile = process.env.ADVISOR_READY_FILE;
    if (readyFile) {
      writeFileSync(readyFile, lastRunFailed ? "Error\n" : "Ready\n", { mode: 0o600 });
    }
    if (ctx.hasUI) {
      ctx.ui.notify(
        lastRunFailed ? "Pi turn failed; Advisor will stop this run" : "Pi turn settled; Advisor is validating output",
        lastRunFailed ? "error" : "info",
      );
    }
  });
}
