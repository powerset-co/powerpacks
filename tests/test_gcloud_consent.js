const { test } = require("node:test");
const assert = require("node:assert/strict");
const { GCLOUD_CLIENT, consentAction, validateRequest } = require("../packs/ingestion/primitives/setup/automations/gcloud_consent.js");
const request = {
  email: "casey@example.com",
  url: `https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=${GCLOUD_CLIENT}&redirect_uri=http%3A%2F%2Flocalhost%3A8085%2F&scope=openid&state=SYNTHETIC_STATE`,
};
const page = { url: "https://accounts.google.com/signin/oauth/consent", text: "", accounts: [], buttons: [], checkboxes: [] };

test("only gcloud's own client with a localhost callback is accepted", () => {
  assert.equal(validateRequest(request), "http://localhost:8085");
  for (const [key, value] of [["client_id", "other"], ["redirect_uri", "https://example.com/"], ["state", ""]]) {
    const url = new URL(request.url);
    url.searchParams.set(key, value);
    assert.throws(() => validateRequest({ ...request, url: url.href }));
  }
});

test("picks the requested account and approves the Cloud SDK; everything else is the user's", () => {
  const chooser = { ...page, text: "Choose an account", accounts: ["other@example.com", request.email] };
  assert.deepEqual(consentAction(chooser, request), { kind: "account", email: request.email });
  assert.equal(consentAction({ ...chooser, accounts: ["other@example.com"] }, request).kind, "human");
  const consent = { ...page, text: "Google Cloud SDK wants to access your Google Account", buttons: ["Cancel", "Allow"] };
  assert.deepEqual(consentAction(consent, request), { kind: "button", name: "Allow" });
  assert.equal(consentAction({ ...consent, text: "Enter your password\nGoogle Cloud SDK" }, request).kind, "human");
  assert.equal(consentAction({ ...consent, text: "Other App wants access" }, request).kind, "human");
  assert.equal(consentAction({ ...consent, url: "https://example.com/" }, request).kind, "human");
});
