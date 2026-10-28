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

// `contents`: files the picker reads (a profile's Preferences, our Chromium's Info.plist).
function picker(files, revisions = [], env = {}, contents = {}) {
  const output = [];
  const context = {
    module: { exports: {} },
    process: { argv: ["node", "browser.js", "--which"], env },
    console: { log: value => output.push(value) },
    require: name => ({
      fs: { existsSync: value => files.includes(value) || value in contents, readdirSync: () => revisions,
            readFileSync: value => contents[value] },
      os: { homedir: () => "/fixture" },
      path,
      // Our Chromium is 148; every installed browser is 156.
      child_process: { execFileSync: binary => binary.includes("for Testing") ? "Google Chrome for Testing 148.0.1.2" : "Google Chrome 156.0.1.2" },
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
  assert.match(browser.desktopUserAgent(), /Chrome\/156\.0\.0\.0/);
  assert.equal(JSON.parse(browser.output[0]).path, apps[i]);
}
// Powerpacks' own Chromium comes before every installed browser, newest revision first.
const ownFirst = picker([chromium, own, ...apps], ["chromium-999", "chromium-1243", "chromium_headless_shell-1243"]);
assert.equal(ownFirst.whichBrowser().name, "Chromium");
assert.equal(ownFirst.whichBrowser().path, chromium);
assert.equal(ownFirst.browserTarget().executablePath, chromium);
assert.match(ownFirst.desktopUserAgent(), /Chrome\/148\.0\.0\.0/);
// A saved profile made by a newer Chrome than our Chromium opens in the installed browser, which
// Chrome requires; an older profile, or one not made yet, opens in ours.
const plist = `${own}/chromium-1243/chrome-mac-arm64/Google Chrome for Testing.app/Contents/Info.plist`;
const profile = "/fixture/.powerpacks/browser-profiles/linkedin";
const saved = (created, last) => ({
  [plist]: "<dict>\n\t<key>CFBundleShortVersionString</key>\n\t<string>148.0.7778.96</string>\n</dict>",
  [`${profile}/Default/Preferences`]: JSON.stringify({ profile: { created_by_version: created }, extensions: { last_chrome_version: last } }),
});
const newer = picker([chromium, own, apps[0]], ["chromium-1243"], {}, saved("156.0.0.0", "156.0.0.0"));
assert.equal(newer.browserTarget(profile).executablePath, apps[0]);
assert.match(newer.desktopUserAgent(profile), /Chrome\/156\.0\.0\.0/);
assert.equal(newer.browserTarget().executablePath, chromium);
assert.equal(newer.browserTarget("/fixture/.powerpacks/browser-profiles/new").executablePath, chromium);
const older = picker([chromium, own, apps[0]], ["chromium-1243"], {}, saved("140.0.0.0", "140.0.0.0"));
assert.equal(older.browserTarget(profile).executablePath, chromium);
assert.match(older.desktopUserAgent(profile), /Chrome\/148\.0\.0\.0/);
// The newest Chrome that wrote it counts: made by 140, last opened by 156.
const reopened = picker([chromium, own, apps[0]], ["chromium-1243"], {}, saved("140.0.0.0", "156.0.0.0"));
assert.equal(reopened.browserTarget(profile).executablePath, apps[0]);
// With no installed browser, ours is the only one to try.
assert.equal(picker([chromium, own], ["chromium-1243"], {}, saved("156.0.0.0", "156.0.0.0")).browserTarget(profile).executablePath, chromium);
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
