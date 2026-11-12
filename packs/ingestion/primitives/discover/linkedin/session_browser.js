#!/usr/bin/env node
/* Run LinkedIn API requests inside the signed-in Chrome profile.
 *
 * The plan is a JSON file of requests ({name, url}); each one is fetched from
 * inside the page with the session's own cookies, csrf token and browser
 * fingerprint, exactly as the site's own JavaScript would. The profile given is
 * a CLONE made by linkedin_session.py (never the real one: a launch that cannot
 * decrypt its cookies deletes them). Results go to --out as
 * [{name, status, text}]; stdout gets one JSON summary. A 429 or 999 stops the
 * batch: LinkedIn is saying slow down.
 *
 * Created: 2026-11-11
 */
const fs = require("fs");
const { chromium } = require("playwright-core");
const { browserTarget, desktopUserAgent, ignoredArgs } = require("../../common/browser.js");

const ORIGIN = "https://www.linkedin.com";
const SLOW_DOWN = new Set([429, 999]);

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 2) args[argv[i].replace(/^--/, "")] = argv[i + 1];
  return args;
}

const pause = () => new Promise((resolve) => setTimeout(resolve, 800 + Math.random() * 900));

// Same launch as connections_browser.js: the installed Chrome and the real keychain for a
// profile copied from Chrome, Playwright's own Chromium otherwise.
function launch(profileDir) {
  return chromium.launchPersistentContext(profileDir, {
    ...browserTarget(profileDir),
    headless: true,
    viewport: { width: 1280, height: 900 },
    userAgent: desktopUserAgent(profileDir),
    ...ignoredArgs(profileDir),
    args: ["--disable-blink-features=AutomationControlled", "--disable-infobars", "--disable-extensions"],
  });
}

async function fetchInPage(page, url) {
  return page.evaluate(async (url) => {
    const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)/) || [])[1];
    const response = await fetch(url, {
      credentials: "include",
      // Voyager answers in its normalized shape (data + included); Sales Navigator in plain JSON.
      headers: { "csrf-token": csrf, "x-restli-protocol-version": "2.0.0",
        accept: url.includes("/voyager/") ? "application/vnd.linkedin.normalized+json+2.1" : "application/json" },
    });
    return { status: response.status, text: await response.text() };
  }, url);
}

(async () => {
  const args = parseArgs(process.argv);
  const plan = JSON.parse(fs.readFileSync(args.plan, "utf8"));
  const results = [];
  const summary = { status: "ok", requests: 0, stopped_at: null };
  const context = await launch(args["profile-dir"]);
  try {
    const page = context.pages()[0] || await context.newPage();
    await page.goto(ORIGIN + "/robots.txt", { waitUntil: "domcontentloaded" });
    const signedIn = (await context.cookies(ORIGIN)).some((cookie) => cookie.name === "li_at");
    if (!signedIn) {
      summary.status = "signed_out";
    } else {
      // Sales Navigator answers 403 until its own home page has run once in the session.
      if (plan.some((request) => request.url.includes("/sales-api/"))) {
        await pause();
        await page.goto(ORIGIN + "/sales/home", { waitUntil: "domcontentloaded" });
        await page.waitForTimeout(2500);
        summary.requests += 1;
      }
      for (const request of plan) {
        await pause();
        const result = await fetchInPage(page, request.url);
        summary.requests += 1;
        results.push({ name: request.name, status: result.status, text: result.text });
        if (SLOW_DOWN.has(result.status)) {
          summary.status = "slow_down";
          summary.stopped_at = request.name;
          break;
        }
      }
    }
  } catch (error) {
    summary.status = "error";
    summary.message = error.message;
  } finally {
    fs.writeFileSync(args.out, JSON.stringify(results));
    console.log(JSON.stringify(summary));
    await context.close();
  }
})();
