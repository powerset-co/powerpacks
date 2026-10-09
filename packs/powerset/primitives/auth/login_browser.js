/* Powerset login in the saved Google profile. Interactive login stays human-owned. */
const fs = require("fs");
const { launchChrome } = require("../../../ingestion/primitives/setup/automations/google_oauth_browser.js");
const { returnFocus } = require("../../../ingestion/primitives/common/browser.js");

async function login(request, { launch = launchChrome, focus = returnFocus, progress = console.error } = {}) {
  const callback = new URL(request.url).searchParams.get("redirect_uri");
  const deadline = Date.now() + request.timeoutSeconds * 1000;
  let context;
  let result;
  let human = false;
  let stopped = false;
  const stop = () => {
    stopped = true;
    context?.close().catch(() => {});
  };
  process.once("SIGTERM", stop);
  process.once("SIGINT", stop);
  const open = async headless => {
    context = await launch(request.profileDir, headless);
    const page = context.pages()[0] || await context.newPage();
    page.on("response", response => {
      const url = new URL(response.url());
      if (url.origin + url.pathname === callback) {
        result = response.ok() ? { status: "ok" } : { status: "error", message: "Login callback was rejected." };
      }
    });
    await page.goto(request.url, { waitUntil: "domcontentloaded", timeout: Math.max(1, deadline - Date.now()) });
    return page;
  };
  try {
    let page = await open(true);
    while (Date.now() < deadline && !stopped && !page.isClosed()) {
      if (result) return result;
      const interactive = await page.locator('input:not([type="hidden"]), button, select, a, [role="button"]')
        .evaluateAll(elements => elements.some(element => element.getClientRects().length > 0));
      if (interactive && !human) {
        progress("[powerset/login] Complete sign-in in the Chrome window.");
        await context.close();
        human = true;
        page = await open(false);
      }
      await page.waitForTimeout(100);
    }
    return result || { status: "error", message: stopped || page.isClosed() ? "Login browser closed." : "login timed out" };
  } catch (error) {
    return result || { status: "error", message: error.message === "Install Google Chrome or Brave to continue."
      ? error.message : "Managed Chrome sign-in could not finish. Retry login or use --no-browser." };
  } finally {
    await context?.close().catch(() => {});
    process.removeListener("SIGTERM", stop);
    process.removeListener("SIGINT", stop);
    if (human) focus();
  }
}

module.exports = { login };

if (require.main === module) {
  const request = JSON.parse(fs.readFileSync(0, "utf8"));
  fs.mkdirSync(request.profileDir, { recursive: true });
  login(request).then(payload => {
    process.stdout.write(`${JSON.stringify(payload)}\n`);
    process.exitCode = payload.status === "ok" ? 0 : 1;
  });
}
