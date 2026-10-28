# Prompt Files

Put long prompt files here and reference them from `../config.json`, for example:

```json
{
  "prompts": [
    { "name": "database-review", "file": "prompts/database-review.md" }
  ]
}
```

## Browser UAT Auth Method

For browser UAT that mutates data, use real authenticated browser storage state:

- Staff: `/tmp/meniv-staff-auth.json`
- Customer portal: `/tmp/meniv-customer-auth.json`

Visual-only dev auth bypasses are acceptable for screenshots and layout checks,
but they are not sufficient for Convex mutations or protected upload/download
flows. If a required storage-state file is missing, stop with
`DO-NOT-PROCEED` and ask the operator to run a headed Playwright login capture.

Recommended capture pattern for future sessions:

```js
const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("http://127.0.0.1:3000/signin");
  console.log("Log in, then press Enter in this terminal.");
  process.stdin.resume();
  process.stdin.once("data", async () => {
    await context.storageState({ path: "/tmp/meniv-staff-auth.json" });
    await browser.close();
    process.exit(0);
  });
})();
```

For customer portal UAT, start from the current portal sign-in route and save to
`/tmp/meniv-customer-auth.json`.
