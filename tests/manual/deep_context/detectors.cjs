// Deliberately broken screens prove that the observers fail, not merely record.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {Observation} = require('./observe.cjs');

async function proveDetectors(browser, root) {
  const directory = path.join(root, 'detector-checks');
  fs.mkdirSync(directory);
  const context = await browser.newContext({viewport: {width: 1440, height: 1000},
    recordVideo: {dir: directory, size: {width: 1440, height: 1000}}});
  const page = await context.newPage();
  await page.route('**/*', route => route.fulfill({contentType: 'text/html', body: `
    <style>body{background:rgb(26,22,20);color:white}.stage{position:absolute;left:340px;top:198px;width:760px;height:740px}
    .decision-card{background:rgb(36,33,32);height:100%}.handoff-copy{padding:240px 100px}
    @keyframes flash{from{opacity:0}to{opacity:1}}.flash{animation:flash .2s}</style>
    <main class="stage"><article class="decision-card"><div class="handoff-copy"><h2>Complete</h2></div></article></main>`}));
  const observation = new Observation(page, directory);
  await observation.start();
  const results = [];
  async function reset() {
    await page.goto('http://qa.invalid/'); // Intercepted entirely in this browser.
    await page.waitForTimeout(300);
  }
  try {
    await reset();
    await observation.quiet(200);
    results.push('stable content accepted');
    for (const [name, script, error] of [
      ['card replacement', "const card=document.querySelector('.decision-card');card.replaceWith(card.cloneNode(true))", /replaced content/],
      ['completion flashing', "const copy=document.querySelector('.handoff-copy');copy.replaceWith(copy.cloneNode(true))", /replaced content/],
      ['contents rewritten', "const copy=document.querySelector('.handoff-copy');copy.innerHTML=copy.innerHTML", /rewrote card\/copy/],
      ['opacity flash', "const stage=document.querySelector('.stage');stage.style.opacity='0';setTimeout(()=>stage.style.opacity='1',180)", /visibility|blank compositor/],
      ['copy-only flash', "const copy=document.querySelector('.handoff-copy');copy.style.opacity='0';setTimeout(()=>copy.style.opacity='1',180)", /visibility/],
      ['animation replay', "document.querySelector('.handoff-copy').classList.add('flash')", /replayed an animation/],
      ['navigation loop', 'location.reload()', /navigated\/reloaded/],
    ]) {
      await reset();
      await page.evaluate(source => setTimeout(() => eval(source), 80), script);
      await assert.rejects(() => observation.quiet(350), error, `${name} was not detected`);
      results.push(`${name} rejected`);
    }
    await reset();
    await observation.beginTransition('deliberate-blank');
    await page.evaluate(() => {
      document.querySelector('.stage').style.visibility = 'hidden';
      setTimeout(() => { document.querySelector('.stage').style.visibility = ''; }, 300);
    });
    await page.waitForTimeout(500);
    // A static compositor need only send a hidden frame and a restored frame.
    assert(observation.active.samples.some(frame => !frame.visible), 'Bare canvas was not detected');
    assert.throws(() => observation.endTransition(), /blank compositor/);
    results.push('blank interval rejected');
    fs.writeFileSync(path.join(directory, 'result.json'), JSON.stringify(results, null, 2));
  } finally {
    observation.write();
    const video = page.video();
    await context.close();
    fs.renameSync(await video.path(), path.join(directory, 'flow.webm'));
  }
  return results;
}

module.exports = {proveDetectors};
