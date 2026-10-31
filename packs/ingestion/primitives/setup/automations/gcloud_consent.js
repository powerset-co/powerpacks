/* The Google Cloud CLI's sign-in in the saved Google profile, so setup asks for Google once:
 * `gcloud auth login` prints its consent URL instead of opening a browser (oauth_browser.py
 * login_gcloud), this approves it here, and the Console automation then reuses the session.
 * Credentials and challenges stay human-owned: anything but the account chooser and the
 * Cloud SDK's own Continue/Allow opens the window for the user. */
const { snapshot } = require("./gmail_consent.js");
const { waitUntil } = require("./page_wait.js");

// The Cloud SDK's public OAuth client, which `gcloud auth login` always uses.
const GCLOUD_CLIENT = "32555940559.apps.googleusercontent.com";

function validateRequest(request) {
  const url = new URL(request.url);
  const callback = new URL(url.searchParams.get("redirect_uri") || "about:blank");
  if (url.origin !== "https://accounts.google.com" || url.pathname !== "/o/oauth2/auth" ||
      url.searchParams.get("client_id") !== GCLOUD_CLIENT || !url.searchParams.get("state") ||
      callback.protocol !== "http:" || callback.hostname !== "localhost") {
    throw new Error("Unexpected gcloud OAuth client or callback.");
  }
  return callback.origin;
}

function consentAction(page, request) {
  const url = new URL(page.url);
  if (url.origin !== "https://accounts.google.com") return { kind: "human" };
  const client = url.searchParams.get("client_id");
  if (client && client !== GCLOUD_CLIENT) return { kind: "human" };
  if (/password|verification|2-step|verify it.s you|captcha|couldn.t sign you in/i.test(page.text) ||
      /challenge|\/signin\/v2\/identifier/.test(url.pathname)) return { kind: "human" };
  const email = request.email.toLowerCase();
  const account = page.accounts.find(value => value.toLowerCase() === email);
  if (/Choose an account/i.test(page.text) && account) return { kind: "account", email: account };
  if (/Google Cloud SDK/.test(page.text)) {
    const name = page.buttons.find(value => /^(Continue|Allow)$/i.test(value));
    if (name) return { kind: "button", name };
  }
  return { kind: "human" };
}

async function loginGcloud(request, { launchChrome, progress, returnFocus }) {
  const callback = validateRequest(request);
  const deadline = Date.now() + request.timeoutSeconds * 1000;
  // gcloud's localhost callback took the code; it then redirects to its success page.
  const done = url => url.startsWith(callback) || url.startsWith("https://cloud.google.com/sdk/auth_success");
  let context = await launchChrome(request.profileDir, true);
  let page = context.pages()[0] || await context.newPage();
  let human = false;
  const open = () => page.goto(request.url, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => {});
  try {
    await open();
    for (;;) {
      // Once the window is the person's, nothing is read or clicked for them: the wait is for
      // gcloud's callback, measured (page_wait.js): the page settled, the network alive, the
      // person still there.
      const wait = await waitUntil(page, { done, human, deadlineMs: deadline - Date.now() });
      if (wait.status !== "act") return wait;
      // The page settled and is the script's: read it once, click once, measure again. A read
      // that throws means Google moved on between the settle and the read (a redirect after load).
      let action;
      try {
        action = consentAction(await snapshot(page), request);
      } catch (_) {
        continue;
      }
      if (action.kind === "human") {
        progress("sign_in");
        await context.close();
        context = await launchChrome(request.profileDir, false);
        page = context.pages()[0] || await context.newPage();
        human = true;
        await open();
        continue;
      }
      if (action.kind === "account") {
        await page.locator(`[data-identifier=${JSON.stringify(action.email)}], [data-email=${JSON.stringify(action.email)}]`)
          .first().click({ timeout: 2500 });
      } else {
        await page.getByRole("button", { name: action.name, exact: true }).first().click({ timeout: 2500 });
      }
    }
  } catch (_) {
    return { status: "needs_user_action", message: "Google sign-in could not finish. Retry and complete it in Chrome." };
  } finally {
    await context.close().catch(() => {});
    if (human) returnFocus();
  }
}

module.exports = { GCLOUD_CLIENT, consentAction, loginGcloud, validateRequest };
