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
 * Prints one JSON object on stdout:
 *   {"status": "ok", "connections": [{slug, name, headline, connected_on}],
 *    "loads": n, "stopped": "known" | "end" | "limit"}
 *   {"status": "needs_user_action", "message": ...}   login not finished in time
 *   {"status": "error", "message": ...}
 *
 * Created: 2026-10-03 (scroll and card parsing adapted from
 * stickerdaniel/linkedin-mcp-server#170).
 */

const fs = require("fs");
const { execFileSync } = require("child_process");
const { chromium } = require("playwright-core");
const { returnFocus } = require("../../common/return_focus.js");

const CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/";
const CARD_LINK = 'main a[href*="/in/"]';
const MAC_CHROME_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const FALLBACK_CHROME_MAJOR = 149;
const LOGIN_POLL_MS = 2000;
const PAUSE_MIN_MS = 500;
const PAUSE_JITTER_MS = 1000;
const IDLE_ROUNDS = 4;

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

function chromeUserAgent() {
  let major = FALLBACK_CHROME_MAJOR;
  try {
    const match = /(\d+)\.\d+\.\d+/.exec(execFileSync(MAC_CHROME_BINARY, ["--version"], { encoding: "utf8", timeout: 5000 }));
    if (match) major = Number(match[1]);
  } catch (_) {
    // keep the fallback major
  }
  return `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${major}.0.0.0 Safari/537.36`;
}

function launch(profileDir, headless) {
  return chromium.launchPersistentContext(profileDir, {
    channel: "chrome",
    headless,
    viewport: null,
    userAgent: chromeUserAgent(),
    ignoreDefaultArgs: ["--enable-automation"],
    args: ["--disable-blink-features=AutomationControlled", "--disable-infobars", "--disable-extensions"],
  });
}

async function signedIn(page) {
  await page.goto(CONNECTIONS_URL, { waitUntil: "domcontentloaded" });
  return page.locator(CARD_LINK).first().waitFor({ state: "visible", timeout: 15000 })
    .then(() => true, () => false);
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
// known connections appear, nothing new loads for IDLE_ROUNDS scrolls, or maxLoads.
async function scrollList(page, known, stopAfterKnown, maxLoads) {
  return page.evaluate(async ({ cardLink, known, stopAfterKnown, maxLoads, pauseMin, pauseJitter, idleRounds }) => {
    const knownSet = new Set(known);
    const slugs = () => {
      const seen = new Set();
      for (const a of document.querySelectorAll(cardLink)) {
        const m = (a.getAttribute("href") || "").match(/\/in\/([^/?#]+)/);
        if (m) seen.add(m[1]);
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

    let idle = 0;
    let loads = 0;
    while (loads < maxLoads) {
      if (reachedKnown()) return { loads, stopped: "known" };
      const before = slugs().size;
      const height = scroller.scrollHeight;
      scroller.scrollTop = scroller.scrollHeight;
      const button = showMore();
      if (button) button.click();
      loads += 1;
      await new Promise((resolve) => setTimeout(resolve, pauseMin + Math.random() * pauseJitter));
      idle = scroller.scrollHeight === height && slugs().size === before ? idle + 1 : 0;
      if (idle >= idleRounds) return { loads, stopped: "end" };
    }
    return { loads, stopped: reachedKnown() ? "known" : "limit" };
  }, { cardLink: CARD_LINK, known, stopAfterKnown, maxLoads, pauseMin: PAUSE_MIN_MS,
       pauseJitter: PAUSE_JITTER_MS, idleRounds: IDLE_ROUNDS });
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
      log("logged in; closing the window and reading headless");
      await context.close();
      returnFocus();
      context = await launch(args.profileDir, true);
      page = context.pages()[0] || await context.newPage();
      if (!await signedIn(page)) {
        result({ status: "error", message: "LinkedIn did not keep the login for the headless browser." });
        return;
      }
    }
    log("reading your connections");
    const scrolled = await scrollList(page, known, Number(args.stopAfterKnown), Number(args.maxLoads));
    const connections = await readCards(page);
    log(`read ${connections.length} connections in ${scrolled.loads} loads (${scrolled.stopped})`);
    result({ status: "ok", connections, ...scrolled });
  } finally {
    await context.close().catch(() => {});
  }
}

main().catch((error) => {
  result({ status: "error", message: String(error && error.message ? error.message : error) });
  process.exit(1);
});
