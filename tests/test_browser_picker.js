/* Browser discovery stays consistent across automation and preflight, without launching apps. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../packs/ingestion/primitives/common/browser.js"), "utf8");
const own = "/fixture/.powerpacks/browsers";
const names = ["Google Chrome", "Brave Browser", "Microsoft Edge", "Arc"];
const apps = names.map(name => `/Applications/${name}.app/Contents/MacOS/${name}`);
const chromium = `${own}/chromium-1243/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing`;

function picker(files, revisions = [], env = {}) {
  const output = [];
  const context = {
    module: { exports: {} },
    process: { argv: ["node", "browser.js", "--which"], env },
    console: { log: value => output.push(value) },
    require: name => ({
      fs: { existsSync: value => files.includes(value), readdirSync: () => revisions },
      os: { homedir: () => "/fixture" },
      path,
      child_process: { execFileSync: binary => { assert.equal(binary, files[0]); return "Chrome 153.0.1.2"; } },
    })[name],
  };
  context.require.main = context.module;
  vm.runInNewContext(source, context);
  return { ...context.module.exports, output };
}

for (let i = 0; i < apps.length; i++) {
  const browser = picker(apps.slice(i));
  assert.equal(browser.whichBrowser().name, names[i]);
  assert.equal(browser.browserTarget().executablePath, apps[i]);
  assert.match(browser.desktopUserAgent(), /Chrome\/153\.0\.0\.0/);
  assert.equal(JSON.parse(browser.output[0]).path, apps[i]);
}
// Powerpacks' own Chromium comes before every installed browser, newest revision first.
const ownFirst = picker([chromium, own, ...apps], ["chromium-999", "chromium-1243", "chromium_headless_shell-1243"]);
assert.equal(ownFirst.whichBrowser().name, "Chromium");
assert.equal(ownFirst.whichBrowser().path, chromium);
assert.equal(ownFirst.browserTarget().executablePath, chromium);
assert.match(ownFirst.desktopUserAgent(), /Chrome\/153\.0\.0\.0/);
const oldChromium = `${own}/chromium-999/chrome-mac/Chromium.app/Contents/MacOS/Chromium`;
assert.equal(picker([oldChromium, own], ["chromium-999"]).whichBrowser().path, oldChromium);
// POWERPACKS_BROWSERS_DIR moves the folder; the default one is then not read.
const moved = "/elsewhere/browsers";
const movedChromium = `${moved}/chromium-1243/chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing`;
assert.equal(picker([movedChromium, moved], ["chromium-1243"], { POWERPACKS_BROWSERS_DIR: moved }).whichBrowser().path, movedChromium);
assert.equal(picker([chromium, own], ["chromium-1243"], { POWERPACKS_BROWSERS_DIR: moved }).whichBrowser(), null);
// The ms-playwright cache is not a fallback.
assert.equal(picker(["/fixture/Library/Caches/ms-playwright"], ["chromium-1243"]).whichBrowser(), null);
assert.equal(picker([]).whichBrowser(), null);
assert.equal(picker([]).output[0], "null");
assert.throws(() => picker([]).browserTarget(), /Powerpacks could not download its browser/);
console.log("Browser picker tests passed.");
