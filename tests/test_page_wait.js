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
const { GCLOUD_CLIENT, loginGcloud } = require("../packs/ingestion/primitives/setup/automations/gcloud_consent.js");
const { loginGcloud: loginGcloudBefore } = require("./fixtures/gcloud_consent_before.js");
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
  // Google's real sign-in is several redirects per hop: `server.chain` extra 302s before the page.
  const via = (to, n) => (n > 0 ? `/redirect?n=${n}&to=${encodeURIComponent(to)}` : to);
  if (url.pathname === "/redirect") {
    response.writeHead(302, { Location: via(url.searchParams.get("to"), Number(url.searchParams.get("n")) - 1) });
    response.end();
    return;
  }
  if (incoming.method === "POST") {
    setTimeout(() => {
      // The callback is gcloud's whole localhost origin, so the sign-in pages live on 127.0.0.1.
      response.writeHead(302, { Location: via(next.startsWith("/callback") ? base() + next : next, server.chain) });
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
server.chain = 0;

// Bound on every loopback address: gcloud's callback must be http://localhost.
before(async () => { await new Promise(resolve => server.listen(0, resolve)); });
after(async () => {
  for (const response of held) response.destroy();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
});
const base = () => `http://localhost:${server.address().port}`;

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

// gcloud's sign-in, old loop (before #991) and new, on the same fake multi-hop sign-in. The
// person drives the hops in the visible window; the result says how the wait ended.
async function gcloudSignIn(loginGcloud, conditions) {
  const pages = [];
  const launchChrome = async () => {
    const browser = await chromium.launch({ executablePath, headless: true });
    const context = await browser.newContext();
    // The consent URL is Google's (validateRequest insists); it lands on the fake sign-in,
    // whose origin makes consentAction say "human" on the identifier page.
    await context.route("https://accounts.google.com/**", route => route.fulfill({ status: 302, headers: { Location: `http://127.0.0.1:${server.address().port}/identifier` } }));
    const page = await context.newPage();
    pages.push(page);
    const cdp = await context.newCDPSession(page);
    await cdp.send("Network.enable");
    await cdp.send("Network.emulateNetworkConditions", conditions);
    const close = context.close.bind(context);
    context.close = async () => { await close(); await browser.close(); };
    return context;
  };
  const hops = [];
  let ended = false;
  const person = (async () => {
    // The second window is the person's; the first was headless and closed by then.
    while (pages.length < 2 && !ended) await new Promise(resolve => setTimeout(resolve, 50));
    if (ended) return;
    const page = pages[1];
    try {
      await page.waitForURL("**/identifier");
      for (const [hop, to] of HOPS) {
        await page.fill("input", "casey");
        await page.getByRole("button", { name: "Next" }).click();
        await page.waitForURL(`**${to.split("?")[0]}**`);
        hops.push(hop);
      }
    } catch (_) {
      // The window closed under the person: the old loop threw out and closed it.
    }
  })();
  const request = { email: "casey@example.com", profileDir: "unused", timeoutSeconds: 60,
    url: `https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=${GCLOUD_CLIENT}&redirect_uri=${encodeURIComponent(`${base()}/`)}&scope=openid&state=SYNTHETIC_STATE` };
  const result = await loginGcloud(request, { launchChrome, progress: () => {}, returnFocus: () => {} });
  ended = true;
  await person;
  return { result, launches: pages.length, hops };
}

test("(f) the old gcloud loop (before #991) throws out mid-hop on slow 3G; the new one reaches the callback", { skip }, async t => {
  server.delay = 1500;
  server.chain = 0;
  const before = await gcloudSignIn(loginGcloudBefore, SLOW_3G);
  t.diagnostic(`old loop: ${JSON.stringify(before.result)} after hops ${before.hops.join(",") || "none"}`);
  // The old loop snapshotted every second with no human guard; a snapshot under a navigation
  // threw "Execution context was destroyed" out of it and closed the person's window.
  assert.equal(before.result.status, "needs_user_action");
  assert.match(before.result.message, /could not finish/);
  assert.ok(before.hops.length < HOPS.length, "the window closed under the person mid-hop");
  const after = await gcloudSignIn(loginGcloud, SLOW_3G);
  t.diagnostic(`new loop: ${JSON.stringify(after.result)} after hops ${after.hops.join(",")}`);
  assert.deepEqual(after.result, { status: "ok" });
  assert.deepEqual(after.hops, HOPS.map(([hop]) => hop));
});
