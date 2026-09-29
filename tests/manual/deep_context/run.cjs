#!/usr/bin/env node
// Deliberately outside test discovery: run scripts/test-deep-context-ui.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const readline = require('node:readline');
const {chromium} = require('playwright');
const {Observation} = require('./observe.cjs');
const {flow} = require('./flow.cjs');
const {proveDetectors} = require('./detectors.cjs');

const CASES = [
  {name: 'normal', slow: false, reduced: false, failures: false},
  {name: 'slow', slow: true, reduced: false, failures: false},
  {name: 'reduced-motion', slow: true, reduced: true, failures: false},
  {name: 'save-failures', slow: true, reduced: false, failures: true},
];

function options() {
  const result = {output: null, scenario: null, channel: 'chrome', headed: false};
  for (let index = 2; index < process.argv.length; index++) {
    const arg = process.argv[index];
    if (arg === '--headed') result.headed = true;
    else if (arg === '--help') {
      console.log('scripts/test-deep-context-ui [--case normal|slow|reduced-motion|save-failures] [--output NEW_DIRECTORY] [--channel chrome|chromium] [--headed]');
      process.exit(0);
    } else if (['--case', '--output', '--channel'].includes(arg)) {
      const value = process.argv[++index];
      assert(value, `${arg} needs a value`);
      result[{'--case': 'scenario', '--output': 'output', '--channel': 'channel'}[arg]] = value;
    } else throw new Error(`Unknown option: ${arg}`);
  }
  if (result.scenario) assert(CASES.some(item => item.name === result.scenario), 'Unknown case');
  return result;
}

async function fixture(directory, scenario) {
  const log = fs.createWriteStream(path.join(directory, 'server.log'));
  const child = spawn(process.env.POWERPACKS_QA_PYTHON, [path.join(__dirname, 'server.py'),
    '--data-dir', path.join(directory, 'data'), '--destination-delay-ms', scenario.slow ? '1200' : '0'],
  {cwd: path.resolve(__dirname, '../../..'), stdio: ['ignore', 'pipe', 'pipe']});
  child.stderr.pipe(log);
  child.stdout.pipe(log);
  const lines = readline.createInterface({input: child.stdout});
  try {
    const [line] = await Promise.race([
      once(lines, 'line'),
      once(child, 'exit').then(([code]) => { throw new Error(`Fixture exited ${code}; see server.log`); }),
      new Promise((_, reject) => setTimeout(() => reject(new Error('Fixture startup timed out')), 15000).unref()),
    ]);
    return {child, ...JSON.parse(line)};
  } catch (error) {
    child.kill('SIGTERM');
    throw error;
  } finally {
    lines.close();
  }
}

async function runCase(browser, root, scenario) {
  const directory = path.join(root, scenario.name);
  fs.mkdirSync(directory);
  const server = await fixture(directory, scenario);
  let context, page, observation;
  const result = {name: scenario.name, status: 'failed'};
  try {
    context = await browser.newContext({viewport: {width: 1440, height: 1000},
      reducedMotion: scenario.reduced ? 'reduce' : 'no-preference',
      recordVideo: {dir: directory, size: {width: 1440, height: 1000}}, serviceWorkers: 'block'});
    const external = [];
    await context.route('**/*', route => {
      if (new URL(route.request().url()).origin === server.url) return route.continue();
      external.push(route.request().url());
      return route.abort('blockedbyclient');
    });
    await context.tracing.start({screenshots: true, snapshots: true, sources: true});
    page = await context.newPage();
    page.setDefaultTimeout(12000);
    page.setDefaultNavigationTimeout(15000);
    observation = new Observation(page, directory);
    await observation.start();
    result.database = await flow(page, server.url, observation, {...scenario, directory});
    assert.deepEqual(external, [], 'Browser attempted external requests');
    result.status = 'passed';
  } catch (error) {
    result.error = error.stack;
    if (page) await page.screenshot({path: path.join(directory, 'failure.png')}).catch(() => {});
  } finally {
    try {
      observation?.write();
      if (context) {
        try {
          await context.tracing.stop({path: path.join(directory, 'trace.zip')});
        } finally {
          await context.close();
        }
        const video = page?.video();
        if (video) fs.renameSync(await video.path(), path.join(directory, 'flow.webm'));
      }
    } finally {
      if (server.child.exitCode === null && server.child.signalCode === null) {
        const exited = once(server.child, 'exit');
        server.child.kill('SIGTERM');
        await exited;
      }
      fs.writeFileSync(path.join(directory, 'result.json'), JSON.stringify(result, null, 2));
    }
  }
  return result;
}

async function main() {
  const args = options();
  const reports = path.join(os.homedir(), '.powerpacks', 'qa');
  if (!args.output) fs.mkdirSync(reports, {recursive: true});
  const root = args.output ? path.resolve(args.output) : fs.mkdtempSync(path.join(reports, 'deep-context-ui-'));
  if (args.output) fs.mkdirSync(root); // Refuse to overwrite recordings or SQLite.
  console.log(`Artifacts: ${root}`);
  const browser = await chromium.launch({headless: !args.headed, ...(args.channel === 'chromium' ? {} : {channel: args.channel})});
  const results = [];
  try {
    await proveDetectors(browser, root);
    console.log('Detector checks passed (deliberate blank, replacement, flashing, animation replay, reload).');
    for (const scenario of CASES.filter(item => !args.scenario || item.name === args.scenario)) {
      console.log(`Running ${scenario.name}…`);
      const result = await runCase(browser, root, scenario);
      results.push(result);
      console.log(`${result.status.toUpperCase()} ${scenario.name}${result.error ? `: ${result.error.split('\n')[0]}` : ''}`);
    }
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(root, 'report.json'), JSON.stringify(results, null, 2));
    fs.writeFileSync(path.join(root, 'index.html'), `<!doctype html><meta charset="utf-8"><title>Deep Context UI QA</title>
      <style>body{font:16px system-ui;background:#1a1614;color:#f0eae2;padding:24px}a{color:#fbbf24}video{width:min(100%,960px)}</style>
      <h1>Deep Context UI QA</h1><p>Synthetic data; provider work replaced. Inspect motion as well as automated results.</p>
      <p><a href="detector-checks/flow.webm">Deliberate failure recordings</a> · <a href="detector-checks/result.json">Detector checks</a></p>
      ${results.map(result => `<section><h2>${result.name}: ${result.status}</h2><p><a href="${result.name}/result.json">Assertions</a> ·
        <a href="${result.name}/observations.json">Frames and DOM events</a> · <a href="${result.name}/trace.zip">Playwright trace</a></p>
        <video controls src="${result.name}/flow.webm"></video></section>`).join('\n')}`);
  }
  console.log(`Report: ${path.join(root, 'index.html')}`);
  process.exitCode = results.some(result => result.status !== 'passed') ? 1 : 0;
}

main().catch(error => { console.error(error); process.exitCode = 1; });
