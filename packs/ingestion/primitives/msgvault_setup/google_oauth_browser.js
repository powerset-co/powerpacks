#!/usr/bin/env node
/* Drive Google Console for msgvault OAuth setup.
 *
 * This is intentionally best-effort. Google login, MFA, and anti-abuse screens
 * stay human-controlled in the opened Chrome window; after those screens are
 * cleared, this script tries to finish the routine form work.
 */

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright-core");

const GMAIL_SCOPES = [
  "https://www.googleapis.com/auth/gmail.readonly",
  "https://www.googleapis.com/auth/gmail.modify",
];

class StepError extends Error {
  constructor(stuckAt, message) {
    super(message);
    this.name = "StepError";
    this.stuckAt = stuckAt;
  }
}

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    if (!key.startsWith("--")) continue;
    const name = key.slice(2).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      args[name] = true;
    } else {
      args[name] = next;
      i += 1;
    }
  }
  return args;
}

function parseList(value) {
  if (Array.isArray(value)) return value.flatMap(parseList);
  return String(value || "")
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function result(payload) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
}

function log(message) {
  process.stderr.write(`[local-msg-vault/browser] ${message}\n`);
}

function regexEscape(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function compactText(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

async function visible(locator, timeout = 1200) {
  try {
    await locator.first().waitFor({ state: "visible", timeout });
    return true;
  } catch (_) {
    return false;
  }
}

async function clickFirst(page, candidates, timeout = 1400) {
  for (const candidate of candidates) {
    const label = candidate instanceof RegExp ? candidate.toString() : String(candidate);
    const locators = [
      page.getByRole("button", { name: candidate }),
      page.getByRole("link", { name: candidate }),
      page.getByText(candidate, { exact: false }),
    ];
    for (const locator of locators) {
      if (await visible(locator, timeout)) {
        log(`clicking ${label}`);
        try {
          await locator.first().click({ timeout: 2500 });
        } catch (error) {
          log(`click failed for ${label}: ${error.message}`);
          continue;
        }
        await page.waitForLoadState("domcontentloaded", { timeout: 2500 }).catch(() => {});
        return true;
      }
    }
  }
  return false;
}

async function clickLocator(locator, label, timeout = 2500) {
  if (!(await visible(locator, timeout))) return false;
  log(`clicking ${label}`);
  try {
    await locator.first().click({ timeout: 2500 });
  } catch (error) {
    log(`click failed for ${label}: ${error.message}`);
    return false;
  }
  await locator.first().page().waitForLoadState("domcontentloaded", { timeout: 2500 }).catch(() => {});
  return true;
}

async function clickEmailAccount(page, email, timeout = 2500) {
  if (!email) return false;
  const candidates = [
    page.getByText(email, { exact: false }),
    page.locator(`[data-email="${email}"]`),
    page.locator(`text=${email}`),
  ];
  for (const locator of candidates) {
    if (await visible(locator, timeout)) {
      await locator.first().click();
      await page.waitForLoadState("domcontentloaded").catch(() => {});
      return true;
    }
  }
  return false;
}

async function chooseRadio(page, candidates, timeout = 1500) {
  for (const candidate of candidates) {
    const label = candidate instanceof RegExp ? candidate.toString() : String(candidate);
    const radio = page.getByRole("radio", { name: candidate });
    if (await visible(radio, timeout)) {
      log(`choosing ${label}`);
      try {
        await radio.first().check({ timeout: 2500 });
        return true;
      } catch (_) {
        try {
          await radio.first().click({ timeout: 2500 });
          return true;
        } catch (error) {
          log(`radio failed for ${label}: ${error.message}`);
        }
      }
    }
  }
  return false;
}

async function fillFirst(page, labels, value, timeout = 1200) {
  if (!value) return false;
  for (const label of labels) {
    const locators = [
      page.getByLabel(label, { exact: false }),
      page.getByPlaceholder(label, { exact: false }),
      page.getByRole("textbox", { name: label }),
    ];
    for (const locator of locators) {
      if (await visible(locator, timeout)) {
        log(`filling ${label}`);
        try {
          await locator.first().fill(value, { timeout: 2500 });
          return true;
        } catch (error) {
          log(`fill failed for ${label}: ${error.message}`);
        }
      }
    }
  }
  return false;
}

async function fillScoped(root, labels, value, timeout = 1500) {
  if (!value) return false;
  for (const label of labels) {
    const labelText = label instanceof RegExp ? label.toString() : String(label);
    const locators = [
      root.getByLabel(label, { exact: false }),
      root.getByPlaceholder(label, { exact: false }),
      root.getByRole("textbox", { name: label }),
    ];
    for (const locator of locators) {
      if (await visible(locator, timeout)) {
        log(`filling ${labelText}`);
        try {
          await locator.first().fill(value, { timeout: 2500 });
          return true;
        } catch (error) {
          log(`fill failed for ${labelText}: ${error.message}`);
        }
      }
    }
  }
  const textboxes = root.getByRole("textbox");
  if (await visible(textboxes, timeout)) {
    log("filling first visible textbox");
    await textboxes.first().fill(value, { timeout: 2500 });
    return true;
  }
  return false;
}

async function chooseOption(page, labelCandidates, optionCandidates) {
  for (const label of labelCandidates) {
    const labelText = label instanceof RegExp ? label.toString() : String(label);
    const combo = page.getByRole("combobox", { name: label });
    if (await visible(combo, 1200)) {
      log(`opening ${labelText}`);
      await combo.first().click();
      if (await clickFirst(page, optionCandidates, 1200)) return true;
    }
    const select = page.getByLabel(label, { exact: false });
    if (await visible(select, 1200)) {
      for (const option of optionCandidates) {
        try {
          log(`selecting ${option} for ${labelText}`);
          await select.first().selectOption({ label: option });
          return true;
        } catch (_) {}
      }
    }
  }
  return false;
}

async function selectSupportEmail(page, email) {
  const selects = [
    page.getByRole("combobox", { name: /User support email|Support email/i }),
    page.locator("cfc-select").first(),
  ];
  for (const select of selects) {
    if (!(await visible(select, 1500))) continue;
    log("opening support email selector");
    await select.first().click({ timeout: 2500 }).catch(() => {});
    await page.waitForTimeout(600);
    const overlay = page.locator(".cdk-overlay-pane, .mat-mdc-select-panel, .cfc-overlay-pane").last();
    const scopedEmail = overlay.getByText(email, { exact: false });
    if (await visible(scopedEmail, 1500)) {
      log(`selecting support email ${email}`);
      await scopedEmail.first().click({ timeout: 2500 });
      return true;
    }
    const anyEmail = page.getByText(email, { exact: false });
    if (await visible(anyEmail, 1500)) {
      log(`selecting support email ${email}`);
      await anyEmail.last().click({ timeout: 2500 });
      return true;
    }
  }
  return false;
}

async function clickButton(page, candidates, timeout = 1800) {
  for (const candidate of candidates) {
    const label = candidate instanceof RegExp ? candidate.toString() : String(candidate);
    if (await clickLocator(page.getByRole("button", { name: candidate }), label, timeout)) return true;
  }
  return false;
}

async function waitForHumanLogin(page, email, timeoutMs) {
  const start = Date.now();
  let lastUrl = "";
  while (Date.now() - start < timeoutMs) {
    const url = page.url();
    if (url !== lastUrl) {
      log(`page: ${url}`);
      lastUrl = url;
    }
    if (!url.includes("accounts.google.com") && !url.includes("/signin/")) {
      return true;
    }
    if (await clickEmailAccount(page, email, 900)) {
      log(`selected Google account ${email}`);
    }
    await page.waitForTimeout(1500);
  }
  log("still waiting for Google login");
  return false;
}

async function gotoPage(page, url, label, timeout = 30000) {
  log(`opening ${label}`);
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout });
  } catch (error) {
    log(`open ${label} did not finish cleanly: ${error.message}`);
  }
  await page.waitForLoadState("domcontentloaded", { timeout: 10000 }).catch(() => {});
}

async function fillFirstVisibleInput(page, value, timeout = 2500) {
  const input = page.locator('input:not([type="search"]):not([type="radio"]):not([type="checkbox"])').first();
  if (!(await visible(input, timeout))) return false;
  log("filling app name");
  await input.fill(value, { timeout: 2500 });
  return true;
}

async function chooseAudience(page, audience) {
  const value = audience === "internal" ? "internal" : "external";
  const radio = page.locator(`input[type="radio"][value="${value}"]`);
  if (await visible(radio, 2000)) {
    log(`choosing ${value} audience`);
    await radio.first().check({ force: true, timeout: 2500 });
    return true;
  }
  return chooseRadio(page, [new RegExp(value, "i")], 1500);
}

async function acceptUserDataPolicy(page) {
  const checkbox = page.locator('input[type="checkbox"]').last();
  if (await visible(checkbox, 1500)) {
    log("accepting Google API Services user data policy");
    await checkbox.check({ force: true, timeout: 2500 }).catch(async () => {
      await checkbox.click({ force: true, timeout: 2500 });
    });
  }
  await clickButton(page, [/Continue/i], 1800);
}

async function googleAuthConfigured(page) {
  const body = await page.locator("body").innerText({ timeout: 5000 }).catch(() => "");
  return !/Google Auth Platform not configured yet/i.test(body);
}

async function requireGoogleAuthConfigured(page) {
  if (await googleAuthConfigured(page)) return true;
  throw new StepError("branding.verify", "Google Auth Platform is still not configured.");
}

async function setupConsent(page, project, email, clientName, audience, timeoutMs) {
  const overview = `https://console.cloud.google.com/auth/overview?project=${encodeURIComponent(project)}`;
  await gotoPage(page, overview, "OAuth overview");
  await waitForHumanLogin(page, email, timeoutMs);
  await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});

  log("configuring OAuth app overview");
  if (await googleAuthConfigured(page)) {
    log("Google Auth Platform already configured");
    return;
  }
  const createUrl = `https://console.cloud.google.com/auth/overview/create?project=${encodeURIComponent(project)}`;
  const opened = await clickFirst(page, [/Get started/i, /Configure consent screen/i, /Create app/i], 2500);
  if (!opened || !page.url().includes("/auth/overview/create")) {
    await gotoPage(page, createUrl, "OAuth branding create page");
  }
  await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});

  await fillFirstVisibleInput(page, clientName, 2500);
  await selectSupportEmail(page, email);
  await clickButton(page, [/Next/i], 2500);
  await page.waitForTimeout(1000);

  await chooseAudience(page, audience);
  await clickButton(page, [/Next/i], 2500);
  await page.waitForTimeout(1000);

  await fillFirst(page, [/Text field for emails/i, /Email addresses/i, /Contact email/i], email, 1800);
  await clickButton(page, [/Next/i], 2500);
  await page.waitForTimeout(1000);

  await acceptUserDataPolicy(page);
  await clickButton(page, [/^Create$/i], 2500);
  await page.waitForTimeout(5000);
  await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});
  await requireGoogleAuthConfigured(page);
}

async function modalRoot(page) {
  const dialogs = page.getByRole("dialog");
  if (await visible(dialogs, 1200)) return dialogs.last();
  const overlays = page.locator("mat-dialog-container, .mat-mdc-dialog-container, cfc-panel");
  if (await visible(overlays, 800)) return overlays.last();
  return page;
}

async function clickScopeRow(root, scope) {
  const exact = new RegExp(`^\\s*${regexEscape(scope)}\\s*$`, "i");
  const rowWithScope = root.getByRole("row").filter({ hasText: scope });
  if (await visible(rowWithScope, 1200)) {
    const checkbox = rowWithScope.first().getByRole("checkbox");
    if (await visible(checkbox, 800)) {
      log(`selecting ${scope}`);
      await checkbox.first().check({ timeout: 2500 }).catch(async () => {
        await checkbox.first().click({ timeout: 2500 });
      });
      return true;
    }
    return clickLocator(rowWithScope, scope, 1200);
  }
  const checkbox = root.getByRole("checkbox", { name: new RegExp(regexEscape(scope), "i") });
  if (await visible(checkbox, 1200)) {
    log(`selecting ${scope}`);
    await checkbox.first().check({ timeout: 2500 }).catch(async () => {
      await checkbox.first().click({ timeout: 2500 });
    });
    return true;
  }
  return clickLocator(root.getByText(exact), scope, 1200);
}

async function addScopes(page, project) {
  log("adding Gmail OAuth scopes");
  const scopesUrl = `https://console.cloud.google.com/auth/scopes?project=${encodeURIComponent(project)}`;
  await gotoPage(page, scopesUrl, "OAuth scopes");
  await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});

  const existing = await verifyScopesOnCurrentPage(page);
  if (existing.ok) {
    log("Gmail scopes already present");
    return existing;
  }

  const opened = await clickButton(page, [/Add or remove scopes/i, /Add scopes/i], 4000);
  if (!opened) {
    throw new StepError("scopes.open", "Scope picker was not visible.");
  }

  const root = await modalRoot(page);
  const manualScopes = root.locator('textarea[aria-label="Manually paste scopes"], textarea').first();
  if (await visible(manualScopes, 1800)) {
    log("pasting Gmail scopes");
    await manualScopes.fill(GMAIL_SCOPES.join("\n"), { timeout: 2500 });
    await clickButton(page, [/Add to table/i], 2500);
    await page.waitForTimeout(1500);
  } else {
    for (const scope of GMAIL_SCOPES) {
      await fillScoped(root, [/Filter/i, /Search/i, /Enter property name or value/i], scope, 1800);
      await page.waitForTimeout(600);
      await clickScopeRow(root, scope);
    }
  }
  await clickButton(page, [/Update/i, /Save/i], 1800);
  await page.waitForTimeout(1200);
  await clickButton(page, [/^Save$/i], 2500);
  await page.waitForTimeout(3000);
  await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});
  const verified = await verifyScopes(page, project);
  if (!verified.ok) {
    throw new StepError("scopes.verify", `Missing Gmail scopes after save: ${verified.missing.join(", ")}`);
  }
  return verified;
}

async function addTestUsers(page, project, email, testUsers, timeoutMs) {
  const audienceUrl = `https://console.cloud.google.com/auth/audience?project=${encodeURIComponent(project)}`;
  await gotoPage(page, audienceUrl, "OAuth audience");
  await waitForHumanLogin(page, email, timeoutMs);
  await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});
  await requireGoogleAuthConfigured(page);

  let body = await page.locator("body").innerText({ timeout: 10000 }).catch(() => "");
  let present = testUsers.filter((user) => body.toLowerCase().includes(user.toLowerCase()));
  let missing = testUsers.filter((user) => !present.includes(user));
  if (missing.length === 0) {
    log("OAuth test users already present");
    return { ok: true, expected: testUsers, added: [], already_present: present, present, missing: [] };
  }
  const toAdd = missing.slice();

  const opened = await clickButton(page, [/Add users/i, /Add test users/i], 5000);
  if (!opened) {
    throw new StepError("test_users.open", "Add users was not visible on the OAuth audience page.");
  }
  const root = await modalRoot(page);
  const emailInput = root.locator('input[aria-label="Text field for emails"], textarea').last();
  let filled = false;
  if (await visible(emailInput, 3000)) {
    for (const user of missing) {
      log(`entering OAuth test user ${user}`);
      await emailInput.click({ timeout: 2500 });
      await page.keyboard.type(user, { delay: 15 });
      await page.keyboard.press("Enter");
      await page.waitForTimeout(400);
    }
    filled = true;
  } else {
    filled = await fillScoped(root, [/Text field for emails/i, /Email addresses/i, /^Emails?$/i], missing.join("\n"), 3000);
  }
  if (!filled) {
    throw new StepError("test_users.fill", "Could not find the OAuth test user email field.");
  }

  if (await visible(emailInput, 1000)) {
    await emailInput.press("Enter", { timeout: 1500 }).catch(() => {});
    await page.waitForTimeout(500);
  }

  const dialog = page.getByRole("dialog").last();
  const saveButton = root.getByRole("button", { name: /^Save$/i });
  let saved = await clickLocator(saveButton, "Save", 5000);
  if (saved) {
    await page.waitForTimeout(1500);
  }
  if (saved && (await visible(dialog, 1000))) {
    log("Save did not close the panel; forcing Save click");
    await saveButton.first().click({ timeout: 5000, force: true }).catch(() => {
      saved = false;
    });
  }
  if (!saved) {
    throw new StepError("test_users.save", "Save was not visible after entering OAuth test users.");
  }
  await page.waitForTimeout(3500);
  await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});

  await gotoPage(page, audienceUrl, "OAuth audience verification");
  await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});
  for (const user of testUsers) {
    await page.getByText(user, { exact: false }).first().waitFor({ state: "visible", timeout: 6000 }).catch(() => {});
  }
  body = await page.locator("body").innerText({ timeout: 10000 }).catch(() => "");
  present = testUsers.filter((user) => body.toLowerCase().includes(user.toLowerCase()));
  missing = testUsers.filter((user) => !present.includes(user));
  if (missing.length) {
    throw new StepError("test_users.verify", `Missing OAuth test users after save: ${missing.join(", ")}`);
  }
  return {
    ok: true,
    expected: testUsers,
    added: toAdd,
    already_present: testUsers.filter((user) => !toAdd.includes(user)),
    present,
    missing,
  };
}

async function verifyScopesOnCurrentPage(page) {
  const body = await page.locator("body").innerText({ timeout: 8000 }).catch(() => "");
  const normalized = compactText(body);
  const present = GMAIL_SCOPES.filter((scope) => {
    const suffix = scope.replace("https://www.googleapis.com", "...");
    return normalized.includes(scope) || normalized.includes(suffix);
  });
  return {
    ok: present.length === GMAIL_SCOPES.length,
    expected: GMAIL_SCOPES,
    present,
    missing: GMAIL_SCOPES.filter((scope) => !present.includes(scope)),
  };
}

async function verifyScopes(page, project) {
  const scopesUrl = `https://console.cloud.google.com/auth/scopes?project=${encodeURIComponent(project)}`;
  await gotoPage(page, scopesUrl, "OAuth scopes verification");
  await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});
  return verifyScopesOnCurrentPage(page);
}

function validateDownloadedClientSecret(filePath) {
  try {
    const data = JSON.parse(fs.readFileSync(filePath, "utf8"));
    if (!data || typeof data !== "object" || !data.installed || typeof data.installed !== "object") {
      return { ok: false, message: "downloaded JSON is not an installed-app OAuth client" };
    }
    const clientId = data.installed.client_id || "";
    const clientSecret = data.installed.client_secret || "";
    if (!clientId || !clientSecret) {
      return { ok: false, message: "downloaded JSON is missing client_id or client_secret" };
    }
    return {
      ok: true,
      client_id: clientId,
      redirect_uris: data.installed.redirect_uris || [],
    };
  } catch (error) {
    return { ok: false, message: String(error && error.message ? error.message : error) };
  }
}

async function downloadClientJson(page, downloadDir, stuckAt) {
  const downloadPromise = page.waitForEvent("download", { timeout: 15000 }).catch(() => null);
  const clickedDownload = await clickFirst(page, [/Download JSON/i, /^Download$/i, /Download/i], 3000);
  if (!clickedDownload) return null;
  const download = await downloadPromise;
  if (!download) {
    throw new StepError(stuckAt, "Clicked Download JSON, but no file was downloaded.");
  }
  const suggested = download.suggestedFilename() || "client_secret.json";
  const downloadPath = path.join(downloadDir, suggested.startsWith("client_secret") ? suggested : "client_secret.json");
  await download.saveAs(downloadPath);
  const validation = validateDownloadedClientSecret(downloadPath);
  if (!validation.ok) {
    throw new StepError(stuckAt, validation.message);
  }
  log(`downloaded client secret to ${downloadPath}`);
  return {
    client_secret_path: downloadPath,
    client_id: validation.client_id,
    redirect_uris: validation.redirect_uris,
  };
}

async function existingDesktopClientVisible(page, clientName) {
  const body = await page.locator("body").innerText({ timeout: 8000 }).catch(() => "");
  return body.includes(clientName) && /\bDesktop\b/i.test(body);
}

async function tryDownloadExistingClient(page, clientName, downloadDir) {
  if (!(await existingDesktopClientVisible(page, clientName))) return null;
  log(`OAuth Desktop client ${clientName} already exists`);
  const name = page.getByRole("link", { name: new RegExp(regexEscape(clientName), "i") });
  if (await visible(name, 1500)) {
    await name.first().click({ timeout: 2500 }).catch(() => {});
    await page.waitForTimeout(2000);
  } else {
    const text = page.getByText(clientName, { exact: true });
    if (await visible(text, 1500)) {
      await text.first().click({ timeout: 2500 }).catch(() => {});
      await page.waitForTimeout(2000);
    }
  }
  const downloaded = await downloadClientJson(page, downloadDir, "clients.download_existing");
  if (downloaded) {
    return { ...downloaded, reused_existing_client: true };
  }
  return null;
}

async function createClient(page, project, email, clientName, downloadDir, timeoutMs) {
  const clients = `https://console.cloud.google.com/auth/clients?project=${encodeURIComponent(project)}`;
  const legacyClient = `https://console.cloud.google.com/apis/credentials/oauthclient?project=${encodeURIComponent(project)}`;
  await gotoPage(page, clients, "OAuth clients page");
  await waitForHumanLogin(page, email, timeoutMs);
  await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});

  const existing = await tryDownloadExistingClient(page, clientName, downloadDir);
  if (existing) return existing;

  let opened = await clickFirst(page, [/Create client/i, /Create OAuth client/i, /Create OAuth client ID/i], 3500);
  if (!opened) {
    log("falling back to legacy OAuth client URL");
    await gotoPage(page, legacyClient, "legacy OAuth client URL");
    await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});
  }

  await chooseOption(page, [/Application type/i], [/Desktop app/i, "Desktop app"]);
  await fillFirst(page, [/Name/i], clientName, 2500);
  await clickFirst(page, [/Create/i], 2500);
  await page.waitForTimeout(2500);

  const downloaded = await downloadClientJson(page, downloadDir, "clients.download_new");
  if (!downloaded) {
    throw new StepError("clients.download_new", "OAuth Desktop client was created, but Download JSON was not visible.");
  }
  return { ...downloaded, reused_existing_client: false };
}

async function debugSnapshot(page, downloadDir) {
  const debugPath = path.join(downloadDir, "last-google-oauth-page.png");
  const textPath = path.join(downloadDir, "last-google-oauth-page.txt");
  try {
    await page.screenshot({ path: debugPath, fullPage: true });
  } catch (_) {}
  try {
    const body = await page.locator("body").innerText({ timeout: 5000 });
    fs.writeFileSync(textPath, body, "utf8");
  } catch (_) {}
  return {
    screenshot: fs.existsSync(debugPath) ? debugPath : "",
    text: fs.existsSync(textPath) ? textPath : "",
  };
}

async function failurePayload(error, page, downloadDir, project, clientName) {
  const debug = await debugSnapshot(page, downloadDir);
  let currentUrl = "";
  try {
    currentUrl = page.url();
  } catch (_) {
    currentUrl = "";
  }
  return {
    status: "needs_user_action",
    project,
    oauth_client_name: clientName,
    stuck_at: error && error.stuckAt ? error.stuckAt : "browser.unknown",
    message: String(error && error.message ? error.message : error),
    current_url: currentUrl,
    download_dir: downloadDir,
    debug_screenshot: debug.screenshot,
    debug_text: debug.text,
  };
}

async function main() {
  const args = parseArgs(process.argv);
  const mode = args.mode || "setup";
  const project = args.project;
  const email = args.email;
  const clientName = args.clientName || "local-msg-vault";
  const profileDir = args.profileDir;
  const downloadDir = args.downloadDir;
  const timeoutSeconds = Number(args.timeoutSeconds || "900");
  const timeoutMs = timeoutSeconds * 1000;
  const audience = args.audience || "external";
  const testUsers = parseList(args.testUsers);

  if (!project || !email || !profileDir || !downloadDir) {
    result({ status: "error", message: "missing required browser automation arguments" });
    process.exit(1);
  }
  if (mode === "add-test-users" && testUsers.length === 0) {
    result({ status: "error", message: "missing OAuth test users" });
    process.exit(1);
  }

  fs.mkdirSync(profileDir, { recursive: true });
  fs.mkdirSync(downloadDir, { recursive: true });

  const context = await chromium.launchPersistentContext(profileDir, {
    channel: "chrome",
    headless: false,
    acceptDownloads: true,
    viewport: null,
    args: ["--start-maximized"],
  });
  const page = context.pages()[0] || await context.newPage();
  page.setDefaultTimeout(Math.max(5000, timeoutSeconds * 1000));

  let payload;
  try {
    if (mode === "add-test-users") {
      const added = await addTestUsers(page, project, email, testUsers, timeoutMs);
      payload = {
        status: "ok",
        project,
        oauth_client_name: clientName,
        test_users: added,
        current_url: page.url(),
      };
    } else {
      await setupConsent(page, project, email, clientName, audience, timeoutMs);
      const scopes = await addScopes(page, project);
      const client = await createClient(page, project, email, clientName, downloadDir, timeoutMs);
      if (client && client.client_secret_path) {
        payload = {
          status: "ok",
          project,
          oauth_client_name: clientName,
          client_secret_path: client.client_secret_path,
          client_id: client.client_id,
          redirect_uris: client.redirect_uris || [],
          reused_existing_client: Boolean(client.reused_existing_client),
          checks: {
            branding_configured: true,
            gmail_scopes: scopes,
            desktop_client: {
              exists: true,
              client_id: client.client_id,
              client_secret_json: "installed",
            },
          },
          current_url: page.url(),
        };
      } else {
        payload = await failurePayload(
          new StepError("clients.download", "The browser is open. Finish creating the Desktop OAuth client and download the JSON."),
          page,
          downloadDir,
          project,
          clientName,
        );
      }
    }
  } catch (error) {
    payload = await failurePayload(error, page, downloadDir, project, clientName);
  } finally {
    await context.close().catch(() => {});
  }
  result(payload);
}

main().catch((error) => {
  result({ status: "error", message: String(error && error.message ? error.message : error) });
  process.exit(1);
});
