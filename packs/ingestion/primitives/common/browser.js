/* The one place the Chrome automation scripts pick a browser and act like it.
 *
 * Picks Chrome, Brave, Edge, Arc, then Playwright Chromium.
 * The user agent matches the installed major
 * version so it agrees with the Sec-CH-UA hints the browser sends. After a login
 * window closes, returnFocus brings back the app that started the session:
 * macOS gives every process that app's bundle id in __CFBundleIdentifier
 * (Terminal, Ghostty, the Claude or Codex desktop app).
 *
 * Changelog:
 * 2026-10-09: Share browser discovery with desktop preflight; include cached Chromium.
 * Created: 2026-10-03
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");

const BROWSER_APPS = [
  "Google Chrome", "Brave Browser", "Microsoft Edge", "Arc",
];
const FALLBACK_MAJOR = 149;

function whichBrowser() {
  for (const name of BROWSER_APPS) {
    const executable = `/Applications/${name}.app/Contents/MacOS/${name}`;
    if (fs.existsSync(executable)) return { name, path: executable };
  }
  const cache = path.join(os.homedir(), "Library/Caches/ms-playwright");
  if (!fs.existsSync(cache)) return null;
  const revisions = fs.readdirSync(cache).filter(name => /^chromium-\d+$/.test(name))
    .sort((a, b) => Number(b.split("-")[1]) - Number(a.split("-")[1]));
  for (const revision of revisions) {
    for (const suffix of [
      "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
      "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
      "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    ]) {
      const executable = path.join(cache, revision, suffix);
      if (fs.existsSync(executable)) return { name: "Chromium", path: executable };
    }
  }
  return null;
}

// Spread into launchPersistentContext options.
function browserTarget() {
  const browser = whichBrowser();
  if (browser) return { executablePath: browser.path };
  throw new Error("Install Chrome, Brave, Microsoft Edge, Arc, or Chromium to continue.");
}

function desktopUserAgent() {
  let major = FALLBACK_MAJOR;
  try {
    const binary = browserTarget().executablePath;
    const match = /(\d+)\.\d+\.\d+/.exec(execFileSync(binary, ["--version"], { encoding: "utf8", timeout: 5000 }));
    if (match) major = Number(match[1]);
  } catch (_) {
    // keep the fallback major
  }
  return `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${major}.0.0.0 Safari/537.36`;
}

function returnFocus() {
  const bundle = process.env.__CFBundleIdentifier;
  if (!bundle) return;
  try {
    execFileSync("open", ["-b", bundle], { timeout: 5000 });
  } catch (_) {
    // focus is a nicety; the login already succeeded
  }
}

module.exports = { browserTarget, desktopUserAgent, returnFocus, whichBrowser };

if (require.main === module && process.argv[2] === "--which") {
  console.log(JSON.stringify(whichBrowser()));
}
