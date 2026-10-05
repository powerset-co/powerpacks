#!/usr/bin/env node
/* Read the signed-in user's LinkedIn connections list in Chrome.
 *
 * Runs headless Chrome over a persistent profile, so nothing pops up while the
 * saved session is valid. When LinkedIn asks for a login, it opens the same
 * profile in a visible window for the login only, closes it once the
 * connections list shows, and scrolls headless again. The
 * list is newest-first, so with --stop-after-known N the scroll stops once N
 * connections from --known-file have loaded (0 never stops early).
 *
 * Every scroll is one request to LinkedIn for the next 10 people. Scrolls wait a
 * random 0.5-1.5 s and stop after --max-loads (300 per run: ~3,000 people in
 * ~5 minutes).
 *
 * Scrolling stops when nothing new loads for END_AFTER_MS; that is the end of
 * the list or LinkedIn no longer sending cards, so it also returns the count
 * LinkedIn shows at the top ("total") for the caller to compare. The signed-in
 * user's own profile slug comes from /in/me, which LinkedIn redirects to it.
 *
 * With --login-only it stops once signed in and prints {"status": "ok"}, so the
 * login can be collected up front and the scroll run later, headless.
 *
 * With --export request|fetch it works LinkedIn's data export page instead of the
 * list: request the larger archive (the one with Connections.csv), or download it
 * into --export-dir once LinkedIn has it ready. Prints {"status": "ok", "export":
 * "requested" | "pending" | "downloaded", "path"?}.
 *
 * Prints one JSON object on stdout:
 *   {"status": "ok", "connections": [{slug, name, headline, connected_on}],
 *    "total": n, "loads": n, "stopped": "known" | "end" | "limit", "owner_slug": str}
 *   {"status": "needs_user_action", "message": ...}   login not finished in time
 *   {"status": "error", "message": ...}
 *
 * Created: 2026-10-03 (scroll and card parsing adapted from
 * stickerdaniel/linkedin-mcp-server#170).
 * Changelog: 2026-10-05: returns LinkedIn's "N connections" count; --export
 * requests and downloads LinkedIn's data export after a stalled read.
 */

const fs = require("fs");
const { chromium } = require("playwright-core");
const { browserTarget, desktopUserAgent, returnFocus } = require("../../common/browser.js");

const CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/";
const CARD_LINK = 'main a[href*="/in/"]';
const LOGIN_POLL_MS = 2000;
const PAUSE_MIN_MS = 500;
const PAUSE_JITTER_MS = 1000;
const END_AFTER_MS = 15000;
const OWNER_URL = "https://www.linkedin.com/in/me/";
const EXPORT_URL = "https://www.linkedin.com/mypreferences/d/download-my-data";
// The larger archive is the one that includes Connections.csv.
const LARGER_ARCHIVE = "#fast-file-only-plus-other-data";

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    if (!argv[i].startsWith("--")) continue;
    const name = argv[i].slice(2).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
    args[name] = argv[i + 1];
    i += 1;
  }
  return args;
}

function result(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function log(message) {
  process.stderr.write(`[linkedin/browser] ${message}\n`);
}

// One line the caller reads as progress; everything else on stderr is the log.
function progress(payload) {
  process.stderr.write(`powerpacks-progress ${JSON.stringify(payload)}\n`);
}

function launch(profileDir, headless) {
  return chromium.launchPersistentContext(profileDir, {
    ...browserTarget(),
    headless,
    viewport: null,
    userAgent: desktopUserAgent(),
    ignoreDefaultArgs: ["--enable-automation"],
    args: ["--disable-blink-features=AutomationControlled", "--disable-infobars", "--disable-extensions"],
  });
}

async function signedIn(page) {
  await page.goto(CONNECTIONS_URL, { waitUntil: "domcontentloaded" });
  return page.locator(CARD_LINK).first().waitFor({ state: "visible", timeout: 15000 })
    .then(() => true, () => false);
}

// LinkedIn's own count at the top of the list ("1,234 connections"), read once.
async function shownTotal(page) {
  const text = await page.waitForFunction(() => [...document.querySelectorAll("main *")]
    .map((element) => (element.childElementCount ? "" : element.textContent.trim()))
    .find((text) => /^[\d,.]+ connections?$/i.test(text)), null, { timeout: 15000 })
    .then((handle) => handle.jsonValue(), () => "");
  return text ? Number(text.replace(/\D/g, "")) : null;
}

// LinkedIn's data export: --export request asks for the larger archive unless a
// request is pending; --export fetch downloads it into --export-dir once it is ready.
async function linkedinExport(page, mode, exportDir) {
  await page.goto(EXPORT_URL, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: /Request (archive|pending|new archive)|Download archive/i }).first()
    .waitFor({ state: "visible", timeout: 15000 });
  const download = page.getByRole("button", { name: /^Download archive$/i })
    .or(page.getByRole("link", { name: /^Download archive$/i })).first();
  if (mode === "fetch") {
    if (!await download.isVisible().catch(() => false)) return { status: "ok", export: "pending" };
    const [file] = await Promise.all([page.waitForEvent("download", { timeout: 120000 }), download.click()]);
    const path = `${exportDir}/linkedin-export.zip`;
    await file.saveAs(path);
    return { status: "ok", export: "downloaded", path };
  }
  if (await page.getByRole("button", { name: /Request pending/i }).isVisible().catch(() => false)) {
    return { status: "ok", export: "pending" };
  }
  await page.locator(LARGER_ARCHIVE).check({ force: true });
  await page.getByRole("button", { name: /^Request archive$/i }).click();
  await page.getByRole("button", { name: /Request pending/i }).waitFor({ state: "visible", timeout: 30000 });
  return { status: "ok", export: "requested" };
}

async function waitForConnections(page, deadline) {
  let lastNotice = 0;
  while (Date.now() < deadline) {
    if (await page.locator(CARD_LINK).first().isVisible().catch(() => false)) return true;
    const url = page.url();
    const signingIn = /\/(login|checkpoint|uas|authwall|signup)/.test(url);
    if (!signingIn && !url.startsWith(CONNECTIONS_URL)) {
      await page.goto(CONNECTIONS_URL, { waitUntil: "domcontentloaded" }).catch(() => {});
    }
    if (signingIn && Date.now() - lastNotice > 30000) {
      log("waiting for you to log in to LinkedIn in the Chrome window");
      lastNotice = Date.now();
    }
    await page.waitForTimeout(LOGIN_POLL_MS);
  }
  return false;
}

// Scroll <main> (the list's own scroller, not the page body) until enough
// known connections appear, nothing new loads for endAfter ms, or maxLoads.
async function scrollList(page, known, stopAfterKnown, maxLoads) {
  return page.evaluate(async ({ cardLink, known, stopAfterKnown, maxLoads, pauseMin, pauseJitter, endAfter }) => {
    const knownSet = new Set(known);
    const slugs = () => {
      const seen = new Set();
      for (const a of document.querySelectorAll(cardLink)) {
        const m = (a.getAttribute("href") || "").match(/\/in\/([^/?#]+)/);
        if (m) seen.add(decodeURIComponent(m[1]).toLowerCase());
      }
      return seen;
    };
    const reachedKnown = () => stopAfterKnown > 0
      && [...slugs()].filter((slug) => knownSet.has(slug)).length >= stopAfterKnown;
    const showMore = () => [...document.querySelectorAll("main button")]
      .find((b) => /show more|load more/i.test(b.innerText || ""));

    // The list scrolls inside <main>; scrolling it to the bottom loads the next 10.
    const main = document.querySelector("main");
    const scroller = main.scrollHeight > main.clientHeight ? main : document.scrollingElement;

    let lastGrowth = performance.now();
    let loads = 0;
    while (loads < maxLoads) {
      if (reachedKnown()) return { loads, stopped: "known" };
      const before = slugs().size;
      const height = scroller.scrollHeight;
      scroller.scrollTop = scroller.scrollHeight;
      const button = showMore();
      if (button) button.click();
      loads += 1;
      if (loads % 5 === 0) window.powerpacksRead(slugs().size);
      await new Promise((resolve) => setTimeout(resolve, pauseMin + Math.random() * pauseJitter));
      if (scroller.scrollHeight !== height || slugs().size !== before) lastGrowth = performance.now();
      else if (performance.now() - lastGrowth >= endAfter) return { loads, stopped: "end" };
    }
    return { loads, stopped: reachedKnown() ? "known" : "limit" };
  }, { cardLink: CARD_LINK, known, stopAfterKnown, maxLoads, pauseMin: PAUSE_MIN_MS,
       pauseJitter: PAUSE_JITTER_MS, endAfter: END_AFTER_MS });
}

// One row per profile: the card's first line is the name, a "Connected on"
// line is the date, and the longest remaining line is the headline.
async function readCards(page) {
  return page.evaluate((cardLink) => {
    const rows = [];
    const seen = new Set();
    for (const a of document.querySelectorAll(cardLink)) {
      const m = (a.getAttribute("href") || "").match(/\/in\/([^/?#]+)/);
      if (!m || seen.has(m[1])) continue;
      seen.add(m[1]);
      const card = a.closest("li") || a.parentElement;
      const lines = (card ? card.innerText : "").split("\n").map((line) => line.trim()).filter(Boolean);
      let connectedOn = "";
      let headline = "";
      for (const line of lines.slice(1)) {
        const date = line.match(/^connected on (.+)$/i);
        if (date) connectedOn = date[1];
        else if (line.length > headline.length) headline = line;
      }
      rows.push({ slug: m[1], name: lines[0] || "", headline, connected_on: connectedOn });
    }
    return rows;
  }, CARD_LINK);
}

async function main() {
  const args = parseArgs(process.argv);
  const known = args.knownFile ? JSON.parse(fs.readFileSync(args.knownFile, "utf8")) : [];
  const deadline = Date.now() + Number(args.timeoutSeconds || "900") * 1000;
  fs.mkdirSync(args.profileDir, { recursive: true });

  let context = await launch(args.profileDir, true);
  try {
    let page = context.pages()[0] || await context.newPage();
    if (!await signedIn(page)) {
      log("LinkedIn needs a login; opening Chrome");
      await context.close();
      context = await launch(args.profileDir, false);
      page = context.pages()[0] || await context.newPage();
      await page.goto(CONNECTIONS_URL, { waitUntil: "domcontentloaded" });
      if (!await waitForConnections(page, deadline)) {
        result({ status: "needs_user_action", message: "Log in to LinkedIn in the Chrome window Powerpacks opened." });
        return;
      }
      log("logged in; closing the window");
      await context.close();
      returnFocus();
      if (args.loginOnly === "1") {
        result({ status: "ok" });
        return;
      }
      context = await launch(args.profileDir, true);
      page = context.pages()[0] || await context.newPage();
      if (!await signedIn(page)) {
        result({ status: "error", message: "LinkedIn did not keep the login for the headless browser." });
        return;
      }
    }
    if (args.loginOnly === "1") {
      result({ status: "ok" });
      return;
    }
    if (args.export) {
      result(await linkedinExport(page, args.export, args.exportDir));
      return;
    }
    const total = await shownTotal(page);
    if (total === null) {
      result({ status: "error", message: "LinkedIn did not show how many connections you have." });
      return;
    }
    log(`reading your connections (LinkedIn shows ${total})`);
    await page.exposeFunction("powerpacksRead", (read) => progress({ read, total }));
    const scrolled = await scrollList(page, known, Number(args.stopAfterKnown), Number(args.maxLoads));
    const connections = await readCards(page);
    log(`read ${connections.length} connections in ${scrolled.loads} loads (${scrolled.stopped})`);
    // LinkedIn's script swaps /in/me for the real slug just after the page loads.
    await page.goto(OWNER_URL, { waitUntil: "domcontentloaded" });
    await page.waitForURL((url) => !/\/in\/me\/?$/.test(new URL(url).pathname), { timeout: 15000 }).catch(() => {});
    const owner = page.url().match(/\/in\/([^/?#]+)/);
    result({ status: "ok", connections, total, ...scrolled, owner_slug: owner && owner[1] !== "me" ? owner[1] : "" });
  } finally {
    await context.close().catch(() => {});
  }
}

main().catch((error) => {
  result({ status: "error", message: String(error && error.message ? error.message : error) });
  process.exit(1);
});
