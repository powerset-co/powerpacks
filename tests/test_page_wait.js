/* The measured waits (page_wait.js) under bad networks, with no real Google: a fake multi-hop
 * sign-in on localhost (identifier → password → 2-step → callback, each hop a form POST with a
 * server-side delay) whose every page holds a long-poll open forever, driven in Powerpacks'
 * Chromium, headless, with CDP network emulation (Slow 3G, offline mid-flow, fast).
 * Run: cd web && node --test ../tests/test_page_wait.js (NODE_PATH points at playwright-core).
 * Created: 2026-10-28 */
const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { browserTarget } = require("../packs/ingestion/primitives/common/browser.js");
const { networkWatch, settled, waitUntil } = require("../packs/ingestion/primitives/setup/automations/page_wait.js");
const { authorize, consentAction } = require("../packs/ingestion/primitives/setup/automations/gmail_consent.js");
const { request, consent } = require("./test_msgvault_consent.js");

let chromium = null;
let executablePath = null;
try {
  ({ chromium } = require("playwright-core"));
  ({ executablePath } = browserTarget());
} catch (error) {
  console.log(`# skipping: ${error.message}`);
}
const skip = chromium && executablePath ? false : "needs playwright-core on NODE_PATH and Powerpacks' Chromium";

const SLOW_3G = { offline: false, latency: 400, downloadThroughput: 400 * 1024 / 8, uploadThroughput: 400 * 1024 / 8 };
const OFFLINE = { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 };
const FAST = { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 };
const HOPS = [["identifier", "/password"], ["password", "/2step"], ["2step", "/callback?state=SYNTHETIC_STATE&code=SYNTHETIC_CODE"]];

// The fake Google: every page long-polls /poll, which never answers, the way Google's pages keep
// a connection open (so `networkidle` never fires). `server.delay` is the per-hop server time.
const held = new Set();
const server = http.createServer((incoming, response) => {
  const url = new URL(incoming.url, "http://localhost");
  if (url.pathname === "/poll") {
    held.add(response);
    return;
  }
  const next = Object.fromEntries(HOPS.map(([hop, to]) => [`/${hop}`, to]))[url.pathname];
  if (incoming.method === "POST") {
    setTimeout(() => {
      response.writeHead(302, { Location: next });
      response.end();
    }, server.delay);
    return;
  }
  response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  const body = url.pathname === "/callback" ? "<h1>Authorization successful!</h1>" :
    `<h1>Synthetic sign-in: ${url.pathname.slice(1)}</h1><form method="post" action="${url.pathname}">
      <input name="value"><button>Next</button></form>`;
  response.end(`<!doctype html><html><body style="font:20px sans-serif;padding:40px">${body}
    <script>fetch("/poll").catch(() => {});</script></body></html>`);
});
server.delay = 0;

before(async () => { await new Promise(resolve => server.listen(0, "127.0.0.1", resolve)); });
after(async () => {
  for (const response of held) response.destroy();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
});
const base = () => `http://127.0.0.1:${server.address().port}`;

async function open(conditions, pathname = "/identifier") {
  const browser = await chromium.launch({ executablePath, headless: true });
  const page = await browser.newPage();
  networkWatch(page);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.enable");
  await cdp.send("Network.emulateNetworkConditions", conditions);
  await page.goto(base() + pathname);
  return { page, cdp, close: () => browser.close() };
}

// The person: fills the page and presses Next.
async function next(page) {
  await page.fill("input", "casey");
  await page.getByRole("button", { name: "Next" }).click();
}

test("(a) slow 3G: the human-phase wait reaches the callback; each hop settles in its load time", { skip }, async t => {
  server.delay = 1500;
  const { page, close } = await open(SLOW_3G);
  try {
    const urls = [];
    const waiting = waitUntil(page, { done: url => url.includes("/callback"), human: true, onProgress: url => urls.push(url) });
    for (const [hop] of HOPS) {
      // The clock starts at the press, not after it: Playwright's click waits for the navigation.
      await page.fill("input", "casey");
      const start = Date.now();
      const pressed = page.getByRole("button", { name: "Next" }).click();
      const shown = await settled(page);
      const waited = Date.now() - start;
      await pressed;
      t.diagnostic(`slow 3G hop ${hop}: settled after ${waited} ms (server delay ${server.delay} ms, latency ${SLOW_3G.latency} ms)`);
      assert.ok(shown !== null && !shown.startsWith(`${base()}/${hop}\n`), `${hop}: settled on the next page`);
      assert.ok(waited >= server.delay, `${hop}: cannot settle before the server answered`);
      assert.ok(waited < server.delay + 4000, `${hop}: settled within the load time, not a cap`);
    }
    const result = await waiting;
    assert.deepEqual(result, { status: "ok" });
    assert.equal(networkWatch(page).lastFailure, null);
    assert.equal(urls.length, 3, `one progress per hop: ${urls.join(" ")}`);
  } finally {
    await close();
  }
});

test("(b) offline after the password hop: the network-dead message in about a minute, not the deadline", { skip }, async t => {
  server.delay = 300;
  const { page, cdp, close } = await open(FAST);
  try {
    const waiting = waitUntil(page, { done: url => url.includes("/callback"), human: true });
    await next(page);
    await settled(page);
    await cdp.send("Network.emulateNetworkConditions", OFFLINE);
    const start = Date.now();
    await next(page);
    const result = await waiting;
    const detected = Date.now() - start;
    t.diagnostic(`offline detected after ${detected} ms (${networkWatch(page).lastFailure.errorText})`);
    assert.equal(result.status, "needs_user_action");
    assert.match(result.message, /cannot reach Google: check the network/);
    assert.ok(detected >= 60000 && detected < 75000, `detected in ${detected} ms`);
  } finally {
    await close();
  }
});

test("(c) nothing happens on a settled page: the idle message", { skip }, async t => {
  const { page, close } = await open(FAST);
  try {
    const start = Date.now();
    const result = await waitUntil(page, { done: () => false, human: true, idleMs: 2000 });
    t.diagnostic(`idle reported after ${Date.now() - start} ms (idleMs 2000)`);
    assert.equal(result.status, "needs_user_action");
    assert.match(result.message, /did not finish: nothing happened/);
    assert.ok(Date.now() - start < 5000);
  } finally {
    await close();
  }
});

test("(d) headless consent clicks exactly what consentAction chooses", { skip }, async () => {
  const clicked = [];
  const launches = [];
  // consentAction only acts on accounts.google.com, so the fixture pages are served by route.
  const launchChrome = async (_profile, headless) => {
    launches.push(headless);
    const browser = await chromium.launch({ executablePath, headless: true });
    const context = await browser.newContext();
    await context.exposeBinding("fixtureClicked", (_, value) => clicked.push(value));
    const lines = consent.text.split("\n").map(line => `<p>${line}</p>`).join("");
    await context.route("**/*", route => {
      const url = new URL(route.request().url());
      const body = url.origin === "http://localhost:8089" ? "Authorization successful!" :
        url.pathname === "/o/oauth2/auth" ? `<h1>Choose an account</h1><button data-identifier="casey@example.com"
          onclick="fixtureClicked('account');location.href='${consent.url}'">Saved Google account</button>` :
        `${lines}<button onclick="fixtureClicked('Continue');location.href='http://localhost:8089/callback?state=SYNTHETIC_STATE&code=SYNTHETIC_CODE'">Continue</button>`;
      return route.fulfill({ contentType: "text/html; charset=utf-8", body: `<!doctype html><html><body>${body}</body></html>` });
    });
    const close = context.close.bind(context);
    context.close = async () => { await close(); await browser.close(); };
    return context;
  };
  const result = await authorize({ ...request, profileDir: "unused", timeoutSeconds: 30 },
    { launchChrome, progress: () => {}, returnFocus: () => {} });
  assert.deepEqual(result, { status: "ok" });
  assert.deepEqual(launches, [true]);
  assert.deepEqual(clicked, ["account", consentAction(consent, request).name]);
});

test("(e) a page holding a long-poll open: settled resolves in under 3 s where networkidle never fires", { skip }, async t => {
  const { page, close } = await open(FAST);
  try {
    const start = Date.now();
    await assert.rejects(page.waitForLoadState("networkidle", { timeout: 3000 }), /Timeout/);
    const idle = Date.now() - start;
    const again = Date.now();
    assert.notEqual(await settled(page), null);
    const waited = Date.now() - again;
    t.diagnostic(`long-poll page: networkidle gave up after ${idle} ms, settled in ${waited} ms`);
    assert.ok(waited < 3000);
  } finally {
    await close();
  }
});
