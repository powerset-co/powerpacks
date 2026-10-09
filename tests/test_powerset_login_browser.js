/* Real Chrome against local OAuth fixtures; no account, provider, or credential writes. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const { login } = require("../packs/powerset/primitives/auth/login_browser.js");
const { launchChrome } = require("../packs/ingestion/primitives/setup/automations/google_oauth_browser.js");

async function fixture(mode) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "powerset-login-fixture-"));
  const launches = [];
  const contexts = [];
  let requests = 0;
  let callbackReceived = false;
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, `http://127.0.0.1:${server.address().port}`);
    if (url.pathname === "/callback") {
      callbackReceived = true;
      assert.equal(url.searchParams.get("state"), "synthetic-state");
      response.writeHead(mode === "callback-error" ? 400 : 200);
      response.end(mode === "callback-error" ? "Login failed" : "You're in.");
      return;
    }
    if (url.pathname === "/google") {
      const step = Number(url.searchParams.get("step") || 0);
      const next = step < 60 ? `/google?step=${step + 1}` : "/callback?state=synthetic-state&code=synthetic-code";
      response.writeHead(200, { "Content-Type": "text/html" });
      response.end(`<script>setTimeout(() => location.href = ${JSON.stringify(next)}, 1)</script>`);
      return;
    }
    if (mode === "google-navigation") {
      response.writeHead(200, { "Content-Type": "text/html" });
      response.end('<button onclick="location.href=\'/google\'">Continue with Google</button>');
      return;
    }
    requests += 1;
    if (mode === "slow-redirect") await new Promise(resolve => setTimeout(resolve, 1500));
    if (mode === "navigation-error") {
      response.destroy();
      return;
    }
    if (mode === "browser-close") {
      response.writeHead(200, { "Content-Type": "text/html" });
      response.end("<h1>Synthetic login page closed by its owner</h1>");
      return;
    }
    if (mode === "timeout" || mode === "human" && requests === 1) {
      response.writeHead(200, { "Content-Type": "text/html" });
      response.end("<h1>Synthetic login</h1><input type=password><button>Sign in</button>");
      return;
    }
    if (mode === "human") {
      const callback = `${url.searchParams.get("redirect_uri")}?code=synthetic-code&state=synthetic-state`;
      response.writeHead(200, { "Content-Type": "text/html" });
      response.end(`<h1>Synthetic Powerpacks sign-in</h1><button onclick="location.href='${callback}'">Sign in</button>
        <script>setTimeout(() => document.querySelector('button').click(), 500)</script>`);
      return;
    }
    response.writeHead(302, { Location: `${url.searchParams.get("redirect_uri")}?code=synthetic-code&state=synthetic-state` });
    response.end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  try {
    const result = await login({
      url: `${origin}/authorize?redirect_uri=${encodeURIComponent(`${origin}/callback`)}&state=synthetic-state`,
      profileDir: profile, timeoutSeconds: mode === "timeout" ? 3 : 10,
    }, {
      launch: async (directory, headless) => {
        launches.push(headless);
        // Opt in to an actual visible synthetic login; CI fixtures remain headless.
        const context = await launchChrome(directory, process.env.POWERSET_FIXTURE_HEADED ? headless : true);
        contexts.push(context);
        if (mode === "google-navigation" && !headless) {
          const page = context.pages()[0];
          page.once("domcontentloaded", () => page.getByRole("button").click().catch(() => {}));
        }
        if (mode === "browser-close") setTimeout(() => context.pages()[0].close(), 500);
        return context;
      },
      focus: () => {}, progress: () => {},
    });
    const reopened = await launchChrome(profile, true);
    await reopened.close();
    assert.ok(contexts.every(context => !context.browser().isConnected()), "Chrome context is still running");
    return { result, launches, callbackReceived };
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(profile, { recursive: true, force: true });
  }
}

test("saved session callback completes headlessly and releases managed profile", async () => {
  const result = await fixture("saved");
  assert.equal(result.result.status, "ok");
  assert.deepEqual(result.launches, [true]);
  assert.equal(result.callbackReceived, true);
});

test("slow OAuth redirect stays headless", async () => {
  const result = await fixture("slow-redirect");
  assert.equal(result.result.status, "ok");
  assert.deepEqual(result.launches, [true]);
});

test("login controls trigger visible handoff in the same profile", async () => {
  const result = await fixture("human");
  assert.equal(result.result.status, "ok");
  assert.deepEqual(result.launches, [true, false]);
});

test("rejected callback closes Chrome without reporting success", async () => {
  const result = await fixture("callback-error");
  assert.equal(result.result.status, "error");
  assert.equal(result.callbackReceived, true);
  assert.deepEqual(result.launches, [true]);
  assert.equal(JSON.stringify(result.result).includes("synthetic-code"), false);
});

test("human login timeout closes Chrome and releases its profile", async () => {
  const result = await fixture("timeout");
  assert.equal(result.result.status, "error");
  assert.equal(result.result.message, "login timed out");
  assert.deepEqual(result.launches, [true, false]);
  assert.equal(result.callbackReceived, false);
});

test("navigation failure closes the owned context", async () => {
  const result = await fixture("navigation-error");
  assert.equal(result.result.status, "error");
  assert.deepEqual(result.launches, [true]);
});

test("closed login window releases its managed profile", async () => {
  const result = await fixture("browser-close");
  assert.equal(result.result.status, "error");
  assert.deepEqual(result.launches, [true]);
});

test("Google sign-in navigations keep the login open until callback", async () => {
  const result = await fixture("google-navigation");
  assert.equal(result.result.status, "ok");
  assert.equal(result.callbackReceived, true);
  assert.deepEqual(result.launches, [true, false]);
});
