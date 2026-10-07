const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("playwright-core");
const { authorize } = require("../packs/ingestion/primitives/setup/automations/gmail_consent.js");
const { request } = require("./test_msgvault_consent.js");

async function fixture(mode) {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "gmail-consent-fixture-"));
  const launches = [];
  const stages = [];
  const clicked = [];
  let seenConsent = false;
  const consent = mode === "existing-grant" ? "https://accounts.google.com/signin/oauth/v3/consent" :
    "https://accounts.google.com/signin/oauth/consent";
  const callback = "http://localhost:8089/callback?state=SYNTHETIC_STATE&code=SYNTHETIC_CODE";
  const recordings = process.env.MSGVAULT_FIXTURE_RECORDINGS;
  const launchChrome = async (_profile, headless) => {
    launches.push(headless);
    const recordDir = recordings && path.join(recordings, mode);
    if (recordDir) fs.mkdirSync(recordDir, { recursive: true });
    const browser = await chromium.launch({ channel: "chrome", headless: true });
    const context = await browser.newContext({ ...(recordDir ? { recordVideo: { dir: recordDir } } : {}) });
    await context.exposeBinding("fixtureClicked", (_, value) => clicked.push(value));
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      let body;
      if (url.origin === "http://localhost:8089") {
        body = mode === "callback-error" ? "Error: no authorization code received" : "Authorization successful!";
      } else if (url.pathname === "/o/oauth2/auth") {
        if (mode.startsWith("test-app-warning")) {
          await route.fulfill({ contentType: "text/html; charset=utf-8",
            body: '<script>location.href="https://accounts.google.com/signin/oauth/warning"</script>' });
          return;
        } else if (mode === "login" && headless || mode === "timeout") {
          body = "<h1>Verify it's you</h1><p>casey@example.com</p><input type=password>";
        } else {
          body = `<h1>Choose an account</h1><button data-identifier="casey@example.com"
            onclick="fixtureClicked('account');location.href='${consent}'">Saved Google account</button>`;
        }
      } else if (url.pathname === "/signin/oauth/warning") {
        body = `<h1>Google hasn’t verified this app</h1><p>You’ve been given access to an app that’s currently being tested.
          You should only continue if you know the developer that invited you.</p><button>Back to safety</button>
          <button onclick="fixtureClicked('test-app');location.href='${consent}'">Continue</button>`;
      } else if (mode === "existing-grant") {
        body = `<h1>local-msg-vault wants access to your Google Account</h1><p>casey@example.com</p>
          <p>local-msg-vault already has some access</p><p>See the 2 services that local-msg-vault has some access to.</p>
          <p>Make sure you trust local-msg-vault</p><button>Cancel</button>
          <button onclick="fixtureClicked('continue');location.href='${callback}'">Continue</button>`;
      } else {
        seenConsent = true;
        const email = mode.includes("wrong-account") ? "other@example.com" : "casey@example.com";
        const permission = mode === "broad-scope" ? "Read, compose, send, and permanently delete all your email from Gmail" :
          "Read, compose, and send emails from your Gmail account";
        body = `<h1>local-msg-vault wants access to your Google Account</h1><p>${email}</p>
          <label><input type=checkbox aria-label="View your email messages and settings">View your email messages and settings</label>
          <label><input type=checkbox aria-label="${permission}">${permission}</label>
          <button onclick="fixtureClicked('continue');location.href='${callback}'">Continue</button>`;
      }
      await route.fulfill({ contentType: "text/html; charset=utf-8", body: `<!doctype html><html><body style="font:24px sans-serif;padding:48px">
        <p>Synthetic Google OAuth fixture — no real account or token</p>${body}</body></html>` });
    });
    const close = context.close.bind(context);
    context.close = async () => { await close(); await browser.close(); };
    return context;
  };
  try {
    const result = await authorize({ ...request, profileDir: temporary, timeoutSeconds: mode === "timeout" || mode.includes("wrong-account") || mode === "broad-scope" ? 3 : 8 },
      { launchChrome, progress: stage => stages.push(stage), returnFocus: () => {} });
    return { result, launches, stages, clicked, seenConsent };
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

test("saved Google account completes exact Gmail consent and callback headlessly", async () => {
  const result = await fixture("saved-login");
  assert.equal(result.result.status, "ok");
  assert.deepEqual(result.launches, [true]);
  assert.deepEqual(result.clicked, ["account", "continue"]);
});

test("expired login hands off to a visible profile and continues when human finishes", async () => {
  const result = await fixture("login");
  assert.equal(result.result.status, "ok");
  assert.deepEqual(result.launches, [true, false]);
  assert.deepEqual(result.stages, ["sign_in"]);
});

test("the observed test-app Continue reaches account-checked Gmail consent", async () => {
  const result = await fixture("test-app-warning");
  assert.equal(result.result.status, "ok");
  assert.deepEqual(result.launches, [true]);
  assert.deepEqual(result.clicked, ["test-app", "continue"]);
  const wrongAccount = await fixture("test-app-warning-wrong-account");
  assert.equal(wrongAccount.result.status, "needs_user_action");
  assert.equal(wrongAccount.clicked.includes("continue"), false);
});

test("the observed prior grant renews two Gmail permissions without new checkboxes", async () => {
  const result = await fixture("existing-grant");
  assert.equal(result.result.status, "ok");
  assert.deepEqual(result.launches, [true]);
  assert.deepEqual(result.clicked, ["account", "continue"]);
});

test("MFA timeout preserves human ownership; retry can complete", async () => {
  const result = await fixture("timeout");
  assert.equal(result.result.status, "needs_user_action");
  assert.deepEqual(result.launches, [true, false]);
  assert.deepEqual(result.clicked, []);
  assert.equal((await fixture("saved-login")).result.status, "ok");
});

test("wrong account and broader permission labels cannot approve consent", async () => {
  for (const mode of ["wrong-account", "broad-scope"]) {
    const result = await fixture(mode);
    assert.equal(result.result.status, "needs_user_action");
    assert.equal(result.clicked.includes("continue"), false);
    assert.equal(result.seenConsent, true);
  }
});

test("a rejected callback is not authorization success", async () => {
  const result = await fixture("callback-error");
  assert.equal(result.result.status, "error");
  assert.equal(JSON.stringify(result.result).includes("SYNTHETIC_CODE"), false);
  assert.equal(JSON.stringify(result.result).includes("SYNTHETIC_STATE"), false);
});
