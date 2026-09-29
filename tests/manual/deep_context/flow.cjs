const assert = require('node:assert/strict');
const path = require('node:path');

const RETARGET_URL = 'https://www.linkedin.com/in/qa-morgan-example';
const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function status(page, base) {
  const response = await page.request.get(`${base}/__qa/status`);
  assert(response.ok());
  return response.json();
}

async function settled(page) {
  await page.waitForTimeout(400);
}

async function sameCard(page, card) {
  assert(await card.evaluate(node => node.isConnected && node === document.querySelector('.decision-card')),
    'Decision replaced the card frame instead of swapping its contents');
  assert.equal(await page.locator('.card-loading').count(), 0, 'Loading placeholder replaced the card');
}

async function person(page) {
  return page.locator('.decision-card [data-parent]').first().getAttribute('data-parent');
}

async function failSave(page, base, button, card, observation) {
  const before = await status(page, base);
  const parent = await person(page);
  await page.request.post(`${base}/__qa/fail-next-save`);
  await observation.beginTransition(`failed-save-${parent}`);
  await button.click();
  await page.locator('.toast.error').filter({hasText: 'Injected save failure'}).waitFor();
  await settled(page);
  assert.equal(await person(page), parent, 'Failed save did not restore the same person');
  await sameCard(page, card);
  observation.endTransition();
  const after = await status(page, base);
  assert.deepEqual(after.worth, before.worth, 'Failed save changed Worth decisions');
  assert.deepEqual(after.links, before.links, 'Failed save changed LinkedIn decisions');
  assert(await button.isEnabled(), 'Failed save left buttons disabled');
}

async function flow(page, base, observation, options) {
  const {directory, failures, slow} = options;
  const shot = name => page.screenshot({path: path.join(directory, `${name}.png`)});
  if (slow) {
    // Delay the response, not the SQLite commit. Browser still uses the real route.
    await page.route('**/*', async route => {
      const request = route.request();
      if (request.method() === 'POST' && ['/worth', '/decide'].includes(new URL(request.url()).pathname)) {
        const response = await route.fetch();
        await wait(450);
        return route.fulfill({response});
      }
      return route.fallback();
    });
  }
  await page.goto(`${base}/?stage=worth`);
  await page.locator('.worth-card').waitFor();
  await settled(page);
  await shot('01-worth');
  await observation.quiet(500);
  const worthCard = await page.locator('.decision-card').elementHandle();
  const worthSeen = new Set();
  if (failures) await failSave(page, base, page.getByRole('button', {name: 'Yes', exact: true}), worthCard, observation);
  for (let index = 0; index < 5; index++) {
    const parent = await person(page);
    assert(!worthSeen.has(parent), `Worth queue looped back to ${parent}`);
    worthSeen.add(parent);
    const decision = parent === 'taylor-echo' ? 'No' : 'Yes';
    await observation.beginTransition(index === 4 ? 'worth-to-enrich' : `worth-${parent}`);
    const response = page.waitForResponse(r => r.url().endsWith('/worth') && r.request().method() === 'POST');
    await page.getByRole('button', {name: decision, exact: true}).click();
    assert.equal((await response).status(), 200);
    if (index < 4) {
      await page.waitForFunction(previous => document.querySelector('.decision-card [data-parent]')?.dataset.parent !== previous, parent);
      await settled(page);
      await sameCard(page, worthCard);
      observation.endTransition();
    }
  }
  await page.waitForURL('**/?stage=enrich');
  await page.getByRole('heading', {name: 'Ready to Enrich', exact: true}).waitFor();
  await settled(page);
  observation.endTransition();
  await shot('02-ready-to-enrich');
  assert.equal(await page.getByText('Decisions Ready', {exact: true}).count(), 0);
  const afterWorth = await status(page, base);
  assert.equal(afterWorth.progress.worth_yes, 4);
  assert.equal(afterWorth.progress.worth_no, 1);
  assert.equal(afterWorth.enrichment_runs, 0, 'Enrichment ran before approval');
  await observation.quiet();

  await page.locator('[data-approve-enrichment]').click();
  await page.getByRole('heading', {name: 'Enriching Contacts', exact: true}).waitFor();
  await shot('03-enriching');
  // Listen before the terminal SSE event so the completion check and navigation
  // are both included in the compositor recording.
  await observation.beginTransition('enrich-to-linkedin');
  await page.waitForURL('**/?stage=linkedin');
  await page.locator('.identity-card').waitFor();
  await settled(page);
  observation.endTransition();
  await shot('04-linkedin');
  const afterEnrich = await status(page, base);
  assert.equal(afterEnrich.enrichment_runs, 1);
  assert.equal(afterEnrich.progress.linkedin_pending, 4, 'Expected exactly the four Worth-Yes candidates');
  const linkedCard = await page.locator('.decision-card').elementHandle();
  if (failures) await failSave(page, base, page.getByRole('button', {name: 'Use this profile', exact: true}), linkedCard, observation);
  const linkedSeen = new Set();
  const expected = {};
  for (const action of ['yes', 'no', 'skip', 'retarget']) {
    const parent = await person(page);
    assert(!linkedSeen.has(parent), `LinkedIn queue looped back to ${parent}`);
    linkedSeen.add(parent);
    expected[`qa-${parent}`] = action;
    if (action === 'no') {
      await page.getByRole('button', {name: 'No', exact: true}).click();
      await page.locator('textarea[name="guidance"]').waitFor();
      assert.equal(await person(page), parent, 'No should open correction for the same person');
      await sameCard(page, linkedCard);
    }
    if (action === 'retarget') {
      await page.locator('.retarget-guidance summary').click();
      await page.locator('textarea[name="guidance"]').fill(RETARGET_URL);
      await shot('05-retarget-draft');
      await observation.beginTransition('linkedin-to-complete');
    } else {
      await observation.beginTransition(`linkedin-${parent}`);
    }
    const response = page.waitForResponse(r => r.url().endsWith('/decide') && r.request().method() === 'POST');
    const button = {
      yes: page.getByRole('button', {name: 'Use this profile', exact: true}),
      no: page.getByRole('button', {name: 'Skip', exact: true}),
      skip: page.getByRole('button', {name: 'Skip', exact: true}),
      retarget: page.getByRole('button', {name: 'Retarget', exact: true}),
    }[action];
    await button.click();
    assert.equal((await response).status(), 200);
    if (action !== 'retarget') {
      await page.waitForFunction(previous => document.querySelector('.decision-card [data-parent]')?.dataset.parent !== previous, parent);
      await settled(page);
      await sameCard(page, linkedCard);
      observation.endTransition();
    }
  }
  await page.locator('.handoff-copy').waitFor();
  await settled(page);
  observation.endTransition();
  await page.getByRole('heading', {name: 'LinkedIn Profiles Checked', exact: true}).waitFor();
  await shot('06-complete');
  // Repeated unchanged events must not replay the check, copy, or navigation.
  await page.request.post(`${base}/__qa/notify`);
  await page.request.post(`${base}/__qa/notify`);
  await observation.quiet(3500);
  const final = await status(page, base);
  assert.equal(final.progress.linkedin_pending, 0);
  assert.deepEqual(final.network_attempts, [], 'Server attempted external network access');
  assert.equal(final.enrichment_runs, 1, 'Enrichment restarted');
  for (const link of final.links) {
    const action = expected[link.parent_id];
    if (!action) {
      assert.equal(link.decision_action, null, 'Worth-No person received a LinkedIn decision');
      continue;
    }
    assert.equal(link.decision_action, {yes: 'verify', no: 'detach', skip: 'detach', retarget: 'retarget'}[action]);
    assert.equal(link.decision_approved, 'yes');
    if (action === 'retarget') assert.equal(link.replacement_url, RETARGET_URL);
  }
  assert.deepEqual(observation.errors, [], 'Browser JavaScript errors');
  assert.equal(observation.events.filter(event => event.kind === 'dom' && event.loading).length, 0);
  assert.equal(observation.events.filter(event => event.kind === 'removed' && event.className.includes('decision-card')).length,
    2, 'Card frames were removed outside the two final decisions');
  assert(!observation.events.some(event => event.heading === 'Decisions Ready'), 'Intermediate Decisions Ready screen flashed');
  // Exactly four documents: Worth, Enrich, LinkedIn, and the final handoff.
  assert.deepEqual(observation.navigations.map(url => new URL(url).searchParams.get('stage')),
    ['worth', 'enrich', 'linkedin', 'linkedin'], 'Unexpected reload/backward navigation/loop');
  const completions = observation.events.filter(event => event.kind === 'dom' && event.completion);
  assert.equal(completions.length, 3, 'Completion check replayed or was skipped');
  const handoffs = observation.events.filter(event => event.kind === 'dom' && event.handoff);
  assert.equal(handoffs.length, 1, 'Completion copy flashed/re-rendered');
  if (options.reduced) {
    assert(!observation.events.some(event => event.kind === 'reveal' && event.crossfade), 'Reduced motion used a crossfade');
    assert(observation.events.filter(event => event.kind === 'animation').every(event =>
      event.duration.split(',').every(duration => parseFloat(duration) <= 0.001)), 'Reduced motion ran a visible animation');
  } else {
    assert.equal(observation.events.filter(event => event.kind === 'reveal' && event.crossfade).length, 3,
      'Stage navigation did not use an actual browser crossfade');
    const durations = observation.events.filter(event => event.kind === 'crossfade-duration');
    assert.equal(durations.length, 3, 'Missing computed crossfade timing');
    assert(durations.every(event => event.old === '0.35s' && event.next === '0.35s'),
      'Browser did not apply the 350ms crossfade');
  }
  return final;
}

module.exports = {flow};
