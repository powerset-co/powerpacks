const { test } = require("node:test");
const assert = require("node:assert/strict");
const { consentAction, validateRequest } = require("../packs/ingestion/primitives/setup/automations/gmail_consent.js");
const request = {
  email: "casey@example.com", clientId: "synthetic.apps.googleusercontent.com", clientName: "local-msg-vault",
  url: "https://accounts.google.com/o/oauth2/auth?client_id=synthetic.apps.googleusercontent.com&login_hint=casey%40example.com&redirect_uri=http%3A%2F%2Flocalhost%3A8089%2Fcallback&state=SYNTHETIC_STATE&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.readonly+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.modify",
};
const consent = {
  url: "https://accounts.google.com/signin/oauth/consent",
  text: "local-msg-vault wants access to your Google Account\ncasey@example.com\nSee your email messages and settings\nRead, compose, and send emails from your Gmail account",
  accounts: [], buttons: ["Continue"], checkboxes: [],
};

test("exact msgvault request is approved; foreign client, account, origin, callback and scope are rejected", () => {
  assert.equal(validateRequest(request), "SYNTHETIC_STATE");
  for (const [key, value] of [["client_id", "other"], ["login_hint", "other@example.com"],
    ["redirect_uri", "https://example.com/callback"], ["scope", "https://mail.google.com/"], ["state", ""]]) {
    const url = new URL(request.url);
    url.searchParams.set(key, value);
    assert.throws(() => validateRequest({ ...request, url: url.href }));
  }
  assert.throws(() => validateRequest({ ...request, url: request.url.replace("accounts.google.com", "example.com") }));
});

test("saved account chooser picks only the requested account", () => {
  const chooser = { ...consent, text: "Choose an account", accounts: ["other@example.com", request.email] };
  assert.deepEqual(consentAction(chooser, request), { kind: "account", email: request.email });
  assert.equal(consentAction({ ...chooser, accounts: ["other@example.com"] }, request).kind, "human");
});

test("the known test-app warning can continue before account and app become visible", () => {
  const warning = { ...consent, url: "https://accounts.google.com/signin/oauth/warning",
    text: "Google hasn’t verified this app\nYou’ve been given access to an app that’s currently being tested. You should only continue if you know the developer that invited you.",
    buttons: ["Back to safety", "Continue"] };
  assert.deepEqual(consentAction(warning, request), { kind: "button", name: "Continue" });
  assert.equal(consentAction({ ...warning, url: "https://accounts.google.com/unknown" }, request).kind, "human");
  assert.equal(consentAction({ ...warning, text: "Google hasn’t verified this app" }, request).kind, "human");
  assert.equal(consentAction({ ...warning, url: warning.url + "?client_id=other" }, request).kind, "human");
});

test("known account consent and only known Gmail checkboxes can be clicked", () => {
  assert.deepEqual(consentAction(consent, request), { kind: "button", name: "Continue" });
  assert.deepEqual(consentAction({ ...consent, checkboxes: [{ name: "See your email messages and settings", checked: false }] }, request),
    { kind: "checkbox", name: "See your email messages and settings" });
  const box = { name: "Read, compose, send, and permanently delete all your email from Gmail", checked: false };
  assert.equal(consentAction({ ...consent, checkboxes: [box] }, request).kind, "human");
});

test("the observed collapsed consent can renew only the existing two Gmail permissions", () => {
  const existing = { ...consent, url: "https://accounts.google.com/signin/oauth/v3/consent",
    text: "local-msg-vault wants access to your Google Account\ncasey@example.com\nlocal-msg-vault already has some access\nSee the 2 services that local-msg-vault has some access to.\nMake sure you trust local-msg-vault" };
  assert.deepEqual(consentAction(existing, request), { kind: "button", name: "Continue" });
  assert.equal(consentAction({ ...existing, text: existing.text.replace("2 services", "3 services") }, request).kind, "human");
  assert.equal(consentAction({ ...existing, text: existing.text.replace(request.email, "other@example.com") }, request).kind, "human");
});

test("password, MFA, captcha, unknown page, different account or app require a human", () => {
  for (const text of ["Password", "2-Step Verification", "CAPTCHA", "Unknown page", consent.text.replace(request.email, "other@example.com"),
    consent.text.replace(request.clientName, "other-client")]) {
    assert.equal(consentAction({ ...consent, text }, request).kind, "human", text);
  }
  for (const url of ["https://example.com/", "https://accounts.google.com/signin/oauth?client_id=other",
    "https://accounts.google.com/signin/oauth?scope=https%3A%2F%2Fmail.google.com%2F"]) {
    assert.equal(consentAction({ ...consent, url }, request).kind, "human");
  }
});

module.exports = { request, consent };
