// DOM identity and compositor pixels catch different failures. Keep both.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {PNG} = require('pngjs');

function observeDocument() {
  const ids = new WeakMap();
  let serial = 0;
  const id = node => {
    if (!node) return null;
    if (!ids.has(node)) ids.set(node, ++serial);
    return ids.get(node);
  };
  const emit = value => window.qaEvent({...value, url: location.href, time: Date.now()});
  let previous = '';
  let previousVisibility = '';
  function painted(node) {
    if (!node) return null;
    let opacity = 1;
    for (let item = node; item; item = item.parentElement) {
      const style = getComputedStyle(item);
      if (style.visibility === 'hidden' || style.display === 'none') return false;
      opacity *= Number(style.opacity);
    }
    return opacity > 0.15;
  }
  function sample() {
    const stage = document.querySelector('.stage');
    if (stage) {
      const card = stage.querySelector('.decision-card');
      const value = {
        kind: 'dom', stage: id(stage), card: id(card),
        person: card?.dataset.parent || card?.querySelector('[data-pub]')?.dataset.pub,
        completion: id(stage.querySelector('.stage-complete')),
        handoff: id(stage.querySelector('.handoff-copy')),
        loading: Boolean(stage.querySelector('.card-loading')),
        heading: stage.querySelector('h2')?.textContent.trim(),
      };
      const current = JSON.stringify(value);
      if (current !== previous) { emit(value); previous = current; }
      const visibility = JSON.stringify([stage, card, stage.querySelector('h2'), stage.querySelector('.handoff-copy')].map(painted));
      if (visibility !== previousVisibility) {
        emit({kind: 'visibility', value: visibility});
        previousVisibility = visibility;
      }
    }
    requestAnimationFrame(sample);
  }
  requestAnimationFrame(sample);
  document.addEventListener('animationstart', event => emit({
    kind: 'animation', name: event.animationName,
    duration: getComputedStyle(event.target).animationDuration,
  }));
  // Detect replacement even when removal/reinsertion happens between paints.
  new MutationObserver(records => {
    for (const record of records) {
      const target = record.target.nodeType === 1 ? record.target : record.target.parentElement;
      if (target?.closest('.decision-card, .handoff-copy')) emit({kind: 'content-mutation'});
      for (const node of record.removedNodes) {
        if (node.nodeType !== 1) continue;
        const selector = '.stage, .decision-card, .handoff-copy';
        const removed = [...node.querySelectorAll(selector)];
        if (node.matches(selector)) removed.push(node);
        removed.forEach(item => emit({kind: 'removed', node: id(item), className: item.className}));
      }
    }
  }).observe(document, {childList: true, characterData: true, subtree: true});
  window.addEventListener('pagereveal', event => {
    emit({kind: 'reveal', crossfade: Boolean(event.viewTransition)});
    if (!event.viewTransition) return;
    requestAnimationFrame(() => emit({kind: 'crossfade-duration',
      old: getComputedStyle(document.documentElement, '::view-transition-old(root)').animationDuration,
      next: getComputedStyle(document.documentElement, '::view-transition-new(root)').animationDuration,
    }));
  });
}

// Inspect the central stage, excluding the sidebar and top bar: those can stay
// visible while the content disappears. A lit pixel here must come from content.
function hasContent(buffer, box) {
  const png = PNG.sync.read(buffer);
  const width = Math.min(600, box.width - 40);
  const height = Math.min(600, box.height - 40);
  const left = Math.max(0, Math.floor(box.x + (box.width - width) / 2));
  const top = Math.max(0, Math.floor(box.y + (box.height - height) / 2));
  let lit = 0;
  let panel = 0;
  for (let y = top; y < Math.min(png.height, top + height); y++) {
    for (let x = left; x < Math.min(png.width, left + width); x++) {
      const at = (y * png.width + x) * 4;
      if (Math.max(png.data[at], png.data[at + 1], png.data[at + 2]) > 80) lit++;
      // Card contents intentionally fade while its charcoal frame stays mounted.
      // That panel is visible content; the bare page canvas is RGB(26,22,20).
      if (png.data[at] >= 33 && png.data[at + 1] >= 30 && png.data[at + 2] >= 29) panel++;
    }
  }
  return lit >= 8 || panel > width * height * 0.5;
}

class Observation {
  constructor(page, directory) {
    this.page = page;
    this.directory = directory;
    this.events = [];
    this.errors = [];
    this.navigations = [];
    this.windows = [];
    this.active = null;
  }

  async start() {
    await this.page.exposeFunction('qaEvent', event => this.events.push(event));
    await this.page.addInitScript(observeDocument);
    this.page.on('pageerror', error => this.errors.push(error.message));
    this.page.on('framenavigated', frame => {
      if (frame === this.page.mainFrame()) this.navigations.push(frame.url());
    });
    this.cdp = await this.page.context().newCDPSession(this.page);
    this.cdp.on('Page.screencastFrame', frame => {
      void this.cdp.send('Page.screencastFrameAck', {sessionId: frame.sessionId}).catch(() => {});
      if (!this.active) return;
      const buffer = Buffer.from(frame.data, 'base64');
      const visible = hasContent(buffer, this.active.box);
      const sample = {time: frame.metadata.timestamp, visible};
      this.active.samples.push(sample);
      if (!visible) {
        sample.file = `${this.active.name}-blank-${this.active.samples.length}.png`;
        fs.writeFileSync(path.join(this.directory, sample.file), buffer);
      }
    });
    await this.cdp.send('Page.startScreencast', {format: 'png', everyNthFrame: 1});
  }

  async beginTransition(name) {
    const box = await this.page.locator('.stage').boundingBox();
    assert(box, 'Stage must exist before measuring a transition');
    this.active = {name, box, samples: []};
    this.windows.push(this.active);
    const buffer = await this.page.screenshot();
    this.active.samples.push({time: Date.now() / 1000, visible: hasContent(buffer, box)});
  }

  endTransition() {
    const window = this.active;
    this.active = null;
    assert(window.samples.length >= 1, `${window.name}: insufficient compositor samples`);
    assert.equal(window.samples.filter(frame => !frame.visible).length, 0,
      `${window.name}: blank compositor frames (see saved PNGs)`);
  }

  async quiet(milliseconds = 1800) {
    // A stable screen must not reload, replace its card/copy, or replay entrances.
    const start = this.events.length;
    const navigationCount = this.navigations.length;
    await this.beginTransition(`idle-${this.windows.length}`);
    await this.page.waitForTimeout(milliseconds);
    assert.equal(this.navigations.length, navigationCount, 'Idle screen navigated/reloaded');
    const events = this.events.slice(start);
    assert.deepEqual(events.filter(event => event.kind === 'removed'), [], 'Idle screen replaced content');
    assert.deepEqual(events.filter(event => event.kind === 'animation'), [], 'Idle screen replayed an animation');
    assert.deepEqual(events.filter(event => event.kind === 'dom'), [], 'Idle screen changed its card, heading or completion');
    assert.deepEqual(events.filter(event => event.kind === 'visibility'), [], 'Idle screen flashed content visibility');
    assert.deepEqual(events.filter(event => event.kind === 'content-mutation'), [], 'Idle screen rewrote card/copy contents');
    this.endTransition();
  }

  write() {
    fs.writeFileSync(path.join(this.directory, 'observations.json'), JSON.stringify({
      events: this.events, errors: this.errors, navigations: this.navigations, transitions: this.windows,
    }, null, 2));
  }
}

module.exports = {Observation, hasContent};
