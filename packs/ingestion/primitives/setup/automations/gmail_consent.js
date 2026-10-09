/* Gmail consent in the existing Google profile. Credentials and challenges stay human-owned. */
const GMAIL_SCOPES = [
  "https://www.googleapis.com/auth/gmail.readonly",
  "https://www.googleapis.com/auth/gmail.modify",
];
const CALLBACK = "http://localhost:8089/callback";
const PERMISSIONS = [
  /^See your email messages and settings\.?$/i,
  /^View your email messages and settings\.?$/i,
  /^Read, compose, and send emails from your Gmail account\.?$/i,
];

function validateRequest(request) {
  const url = new URL(request.url);
  const scopes = url.searchParams.get("scope")?.split(/\s+/).sort();
  if (url.origin !== "https://accounts.google.com" || url.pathname !== "/o/oauth2/auth" ||
      url.searchParams.get("client_id") !== request.clientId ||
      url.searchParams.get("login_hint")?.toLowerCase() !== request.email.toLowerCase() ||
      url.searchParams.get("redirect_uri") !== CALLBACK ||
      !url.searchParams.get("state") ||
      JSON.stringify(scopes) !== JSON.stringify([...GMAIL_SCOPES].sort())) {
    throw new Error("Unexpected msgvault OAuth client, account, scope, or callback.");
  }
  return url.searchParams.get("state");
}

function consentAction(snapshot, request) {
  const url = new URL(snapshot.url);
  if (url.origin !== "https://accounts.google.com") return { kind: "human" };
  const client = url.searchParams.get("client_id");
  if (client && client !== request.clientId) return { kind: "human" };
  const scopes = url.searchParams.get("scope");
  if (scopes && scopes.split(/\s+/).some(scope => !GMAIL_SCOPES.includes(scope))) return { kind: "human" };
  if (/password|verification|2-step|verify it.s you|captcha|couldn.t sign you in/i.test(snapshot.text) ||
      /challenge|\/signin\/v2\/identifier/.test(url.pathname)) return { kind: "human" };
  if (url.pathname === "/signin/oauth/warning" && /Google hasn.t verified this app/.test(snapshot.text) &&
      snapshot.text.includes("You’ve been given access to an app that’s currently being tested.") &&
      snapshot.buttons.includes("Back to safety") && snapshot.buttons.includes("Continue")) {
    return { kind: "button", name: "Continue" };
  }
  const email = request.email.toLowerCase();
  const account = snapshot.accounts.find(value => value.toLowerCase() === email);
  if (/Choose an account/i.test(snapshot.text) && account) return { kind: "account", email: account };
  const emails = snapshot.text.match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || [];
  if (!emails.some(value => value.toLowerCase() === email) ||
      emails.some(value => value.toLowerCase() !== email)) return { kind: "human" };
  if (!snapshot.text.includes(request.clientName)) return { kind: "human" };
  if (/Google hasn.t verified this app/i.test(snapshot.text)) {
    if (snapshot.buttons.includes("Advanced")) return { kind: "button", name: "Advanced" };
    const name = `Go to ${request.clientName} (unsafe)`;
    if (snapshot.buttons.includes(name)) return { kind: "button", name };
    return { kind: "human" };
  }
  if (snapshot.checkboxes.some(box => !PERMISSIONS.some(pattern => pattern.test(box.name)) &&
      !/^Select all$/i.test(box.name))) return { kind: "human" };
  const unchecked = snapshot.checkboxes.find(box => !box.checked && PERMISSIONS.some(pattern => pattern.test(box.name)));
  if (unchecked) return { kind: "checkbox", name: unchecked.name };
  const existingAccess = url.pathname === "/signin/oauth/v3/consent" && snapshot.checkboxes.length === 0 &&
    snapshot.text.includes(`${request.clientName} already has some access`) &&
    snapshot.text.includes(`See the 2 services that ${request.clientName} has some access to.`);
  if (/wants access to your Google Account/i.test(snapshot.text) &&
      !existingAccess &&
      !snapshot.checkboxes.some(box => PERMISSIONS.some(pattern => pattern.test(box.name))) &&
      !PERMISSIONS.some(pattern => snapshot.text.split("\n").some(line => pattern.test(line.trim())))) {
    return { kind: "human" };
  }
  if (/Sign in to|wants access to your Google Account|already has some access/i.test(snapshot.text)) {
    const name = snapshot.buttons.find(value => /^(Continue|Allow)$/i.test(value));
    if (name) return { kind: "button", name };
  }
  return { kind: "human" };
}

async function snapshot(page) {
  return page.evaluate(() => {
    const visible = element => element.getClientRects().length > 0;
    const name = element => element.getAttribute("aria-label") || element.innerText || "";
    return {
      url: location.href,
      text: document.body.innerText,
      accounts: [...document.querySelectorAll("[data-identifier], [data-email]")].filter(visible)
        .map(element => element.getAttribute("data-identifier") || element.getAttribute("data-email")),
      buttons: [...document.querySelectorAll("button, a, [role=button]")].filter(visible).map(name),
      checkboxes: [...document.querySelectorAll("input[type=checkbox], [role=checkbox]")].filter(visible)
        .map(element => ({ name: name(element) || element.labels?.[0]?.innerText || "",
          checked: element.checked || element.getAttribute("aria-checked") === "true" })),
    };
  });
}

async function authorize(request, { launchChrome, progress, returnFocus }) {
  const state = validateRequest(request);
  const deadline = Date.now() + request.timeoutSeconds * 1000;
  let context = await launchChrome(request.profileDir, true);
  let page = context.pages()[0] || await context.newPage();
  let human = false;
  const open = () => page.goto(request.url, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => {});
  try {
    await open();
    while (Date.now() < deadline && !page.isClosed()) {
      const current = new URL(page.url());
      if (current.origin + current.pathname === CALLBACK) {
        const text = await page.locator("body").innerText();
        return current.searchParams.get("state") === state && current.searchParams.has("code") &&
          /Authorization successful!/.test(text)
          ? { status: "ok" } : { status: "error", message: "msgvault rejected the OAuth callback." };
      }
      const action = consentAction(await snapshot(page), request);
      if (action.kind === "human") {
        if (!human) {
          progress("sign_in");
          await context.close();
          context = await launchChrome(request.profileDir, false);
          page = context.pages()[0] || await context.newPage();
          human = true;
          await open();
        }
        await page.waitForTimeout(1000);
        continue;
      }
      if (action.kind === "account") {
        await page.locator(`[data-identifier=${JSON.stringify(action.email)}], [data-email=${JSON.stringify(action.email)}]`)
          .first().click({ timeout: 2500 });
      } else if (action.kind === "checkbox") {
        await page.getByRole("checkbox", { name: action.name, exact: true }).check({ timeout: 2500 });
      } else {
        await page.getByRole("button", { name: action.name, exact: true }).or(
          page.getByRole("link", { name: action.name, exact: true })).first().click({ timeout: 2500 });
      }
      await page.waitForTimeout(500);
    }
    return { status: "needs_user_action", message: "Google sign-in did not finish. Retry authorization to reopen Chrome." };
  } catch (_) {
    return { status: "needs_user_action", message: "Google authorization could not finish. Retry and complete sign-in in Chrome." };
  } finally {
    await context.close().catch(() => {});
    if (human) returnFocus();
  }
}

module.exports = { GMAIL_SCOPES, authorize, consentAction, snapshot, validateRequest };
