/* settle() on a Console-like page that draws its nav, shows a spinner while it fetches, then
 * its content: the read after settle must see the content, not the spinner page. No real Google.
 * Run: node --test tests/test_console_settle.js (NODE_PATH points at playwright-core; set
 * POWERPACKS_TEST_CHROMIUM to a Chromium binary when Powerpacks' own is not installed).
 * Created: 2026-11-16 */
const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");

let chromium = null;
let executablePath = process.env.POWERPACKS_TEST_CHROMIUM || null;
let settle = null;
let spinning = null;
try {
  ({ chromium } = require("playwright-core"));
  ({ settle, spinning } = require("../packs/ingestion/primitives/setup/automations/google_oauth_browser.js"));
  if (!executablePath) ({ executablePath } = require("../packs/ingestion/primitives/common/browser.js").browserTarget());
} catch (error) {
  console.log(`# skipping: ${error.message}`);
}
const skip = chromium && executablePath && settle ? false : "needs playwright-core on NODE_PATH and a Chromium";

// ?ms=N: the nav at once, a spinner for N ms, then the scopes table. ?hidden=1: a progress bar
// kept in the page but hidden, as pages do, which is not loading.
const server = http.createServer((incoming, response) => {
  const url = new URL(incoming.url, "http://localhost");
  const ms = Number(url.searchParams.get("ms") || 0);
  response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  response.end(`<nav>Overview Branding Audience Clients Data Access</nav>
    <div id="main">${url.searchParams.get("hidden")
      ? '<div role="progressbar" aria-hidden="true" style="width:40px;height:40px"></div><p>ready</p>'
      : '<div role="progressbar" style="width:40px;height:40px"></div>'}</div>
    <script>if (${ms}) setTimeout(() => { document.getElementById("main").innerHTML =
      "<table><tr><td>.../auth/gmail.readonly</td></tr></table>"; }, ${ms});</script>`);
});
let browser = null;
const base = () => `http://127.0.0.1:${server.address().port}`;

before(async () => {
  if (skip) return;
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  browser = await chromium.launch({ executablePath, headless: true });
});
after(async () => {
  if (browser) await browser.close();
  server.close();
});

test("settle waits out a spinner that two reads would call settled", { skip }, async () => {
  const page = await browser.newPage();
  await page.goto(`${base()}/?ms=2500`);
  const start = Date.now();
  await settle(page);
  assert.ok(Date.now() - start >= 2000, "returned while the spinner was up");
  assert.match(await page.locator("body").innerText(), /gmail\.readonly/);
  await page.close();
});

test("a hidden progress bar is not loading", { skip }, async () => {
  const page = await browser.newPage();
  await page.goto(`${base()}/?hidden=1`);
  assert.equal(await spinning(page), false);
  const start = Date.now();
  await settle(page);
  assert.ok(Date.now() - start < 3000);
  await page.close();
});

test("a spinner that never goes costs 15 s, not the whole settle", { skip }, async () => {
  const page = await browser.newPage();
  await page.goto(`${base()}/`);
  const start = Date.now();
  await settle(page);
  const took = Date.now() - start;
  assert.ok(took >= 14000 && took < 20000, `took ${took}ms`);
  await page.close();
});
