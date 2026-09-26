// Share page: every person once, filtered client-side; bulk tags in one write.
// Vocabulary: person, worth, share / confirm / not sharing, tag, label, flag.

const API = "/api/share/";
const ROW_H = 40;
const OVERSCAN = 8;
const FILTERS_KEY = "powerpacks:share-filters:v1";
const YEAR = 365;

const root = document.querySelector("[data-share]");
const els = {
  rail: root.querySelector("[data-rail]"),
  head: root.querySelector("[data-head]"),
  quick: root.querySelector("[data-quick]"),
  search: root.querySelector("[data-search]"),
  chips: root.querySelector("[data-chips]"),
  count: root.querySelector("[data-count]"),
  gridHead: root.querySelector("[data-grid-head]"),
  viewport: root.querySelector("[data-viewport]"),
  spacer: root.querySelector("[data-spacer]"),
  rows: root.querySelector("[data-rows]"),
  empty: root.querySelector("[data-empty]"),
  drawer: root.querySelector("[data-drawer]"),
  bulkbar: root.querySelector("[data-bulkbar]"),
  toast: root.querySelector(".toast"),
};

const TEXT = {
  share: { yes: "Share", confirm: "Confirm", no: "Not sharing" },
  reason: {
    worth_yes: "worth yes", worth_maybe: "worth maybe", worth_no: "worth no", owner: "the owner",
    human_share: "you said share", human_private: "you said private",
    family: "family", romantic_partner: "partner", minor: "minor", sensitive_context: "sensitive context",
    sensitive_provider: "clinician / lawyer / banker", automated_sender: "automated sender", stranger: "stranger",
  },
};
const humanize = (value) => String(value ?? "").replace(/^is_/, "").replaceAll("_", " ");
const label = (kind, value) => (TEXT[kind] && TEXT[kind][value]) || humanize(value);

function lastBucket(days) {
  if (days === null || days === undefined) return "never";
  if (days <= YEAR) return "<1y";
  if (days <= 2 * YEAR) return "1–2y";
  return ">2y";
}
function warmthBucket(value) {
  if (value === null || value === undefined) return "";
  if (value < 1) return "0–1 distant";
  if (value < 2) return "1–2 friendly";
  if (value < 3) return "2–3 close";
  return "3–4 inner circle";
}

// Facets: OR within one, AND across. `get` returns the row's values for the facet.
// Default facets stay open; the rest collapse under "More filters".
const FACETS = [
  { key: "share", label: "Decision", get: (r) => [r.share], order: ["yes", "confirm", "no"], text: (v) => label("share", v) },
  { key: "flag", label: "Flag", get: (r) => (r.flag ? [r.flag] : []), text: (v) => label("reason", v) },
  { key: "worth", label: "Worth", get: (r) => [r.worth || "unjudged"], order: ["yes", "maybe", "no", "unjudged"] },
  { key: "relationship_kind", label: "Relationship", get: (r) => (r.relationship_kind ? [r.relationship_kind] : []) },
  { key: "last", label: "Last contact", get: (r) => [r.last], order: ["<1y", "1–2y", ">2y", "never"] },
  { key: "channels", label: "Channels", get: (r) => r.channels, order: ["gmail", "imessage", "whatsapp", "linkedin"] },
  { key: "linkedin", label: "LinkedIn", get: (r) => [r.public_identifier ? "has LinkedIn" : "no LinkedIn"], order: ["has LinkedIn", "no LinkedIn"] },
  { key: "reason", label: "Reason", get: (r) => [r.reason], text: (v) => label("reason", v), more: true },
  { key: "worth_source", label: "Worth decided by", get: (r) => (r.worth_source ? [r.worth_source] : []), more: true },
  { key: "tags", label: "Your tags", get: (r) => r.tags, more: true },
  { key: "labels", label: "Labels ≥ 60%", get: (r) => r.labels, text: humanize, more: true, search: true },
  { key: "function", label: "Function", get: (r) => (r.function ? [r.function] : []), more: true },
  { key: "seniority", label: "Seniority", get: (r) => (r.seniority ? [r.seniority] : []), more: true },
  { key: "mode", label: "Register", get: (r) => (r.mode ? [r.mode] : []), more: true },
  { key: "warmth", label: "Warmth", get: (r) => (r.warmthBucket ? [r.warmthBucket] : []), more: true },
  { key: "direction", label: "Who writes", get: (r) => (r.direction ? [r.direction] : []), more: true },
  { key: "hierarchy", label: "Hierarchy", get: (r) => (r.hierarchy ? [r.hierarchy] : []), more: true },
  { key: "intro_source", label: "How you met", get: (r) => (r.intro_source ? [r.intro_source] : []), more: true },
  { key: "evidence", label: "Evidence", get: (r) => [
    ...(r.linkedin_only ? ["no JEV labels"] : []), ...(r.group_chat_only ? ["group chats only"] : []),
    ...(r.shared_employer ? ["shared employer"] : []), ...(r.shared_school ? ["shared school"] : []),
  ], more: true },
];
const FACET_BY_KEY = new Map(FACETS.map((facet) => [facet.key, facet]));
const VISIBLE_VALUES = 8;

// Quick filters: named facet selections with explicit predicates.
const QUICK = [
  { name: "Needs confirm", set: { share: ["confirm"] } },
  { name: "Family", set: { flag: ["family"] } },
  { name: "Sensitive context", set: { flag: ["sensitive_context"] } },
  { name: "Service providers", set: { relationship_kind: ["service_provider"] } },
  { name: "Recruiters", set: { labels: ["is_recruiter"] } },
  { name: "Strangers / automated", set: { flag: ["stranger", "automated_sender"] } },
  { name: "Last contact > 2y", set: { last: [">2y"] } },
  { name: "Close friends", set: { relationship_kind: ["close_friend"] } },
];

const state = {
  rows: [], byId: new Map(), uploadCommand: "",
  filters: new Map(), text: "", sort: { key: "name", dir: 1 },
  matching: [], counts: new Map(), selected: new Set(), focus: -1,
  drawerId: null, undo: null, saving: false, expanded: new Set(), moreOpen: false, labelSearch: "",
};

// ---------- helpers ----------

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}
function initials(name) {
  const words = String(name || "").split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] || "") + (words.length > 1 ? words[words.length - 1][0] : "")).toUpperCase() || "?";
}
function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 10);
  return date.toLocaleDateString(undefined, { month: "short", year: "numeric" });
}
function announce(message, isError = false) {
  els.toast.textContent = message;
  els.toast.classList.toggle("error", isError);
  els.toast.classList.add("show");
  window.clearTimeout(announce.timer);
  announce.timer = window.setTimeout(() => els.toast.classList.remove("show"), isError ? 6000 : 2200);
}
function saveFilters() {
  const filters = Object.fromEntries([...state.filters].map(([key, values]) => [key, [...values]]));
  try { sessionStorage.setItem(FILTERS_KEY, JSON.stringify({ filters, text: state.text, sort: state.sort })); } catch { /* fine */ }
}
function restoreFilters(hasConfirm) {
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(FILTERS_KEY) || "null"); } catch { /* fresh */ }
  if (saved && saved.filters) {
    state.filters = new Map(Object.entries(saved.filters).map(([key, values]) => [key, new Set(values)]));
    state.text = saved.text || "";
    state.sort = saved.sort || state.sort;
    return;
  }
  if (hasConfirm) state.filters.set("share", new Set(["confirm"]));
}

// ---------- data ----------

async function load() {
  const response = await fetch(`${API}people`);
  if (!response.ok) throw new Error((await response.text()) || "Could not load people");
  const payload = await response.json();
  const { columns } = payload;
  const index = Object.fromEntries(columns.map((column, position) => [column, position]));
  state.rows = payload.rows.map((values) => {
    const row = {};
    for (const column of columns) row[column] = values[index[column]];
    row.last = lastBucket(row.recency_days);
    row.warmthBucket = warmthBucket(row.warmth);
    row.search = `${row.name} ${row.title} ${row.company} ${row.location}`.toLowerCase();
    return row;
  });
  state.byId = new Map(state.rows.map((row) => [row.person_id, row]));
  state.uploadCommand = payload.upload_command || "";
}

function filterRows() {
  const needle = state.text.trim().toLowerCase();
  const active = [...state.filters].filter(([, values]) => values.size);
  const counts = new Map(FACETS.map((facet) => [facet.key, new Map()]));
  const quickCounts = QUICK.map(() => 0);
  const matching = [];
  for (const row of state.rows) {
    if (needle && !row.search.includes(needle)) continue;
    let failed = 0;
    let failedKey = "";
    for (const [key, values] of active) {
      const facet = FACET_BY_KEY.get(key);
      if (!facet.get(row).some((value) => values.has(value))) {
        failed += 1;
        failedKey = key;
        if (failed > 1) break;
      }
    }
    if (failed === 0) matching.push(row);
    // A facet's counts ignore its own selection so the other choices stay discoverable.
    for (const facet of FACETS) {
      if (failed === 1 && facet.key !== failedKey) continue;
      if (failed > 1) break;
      const bucket = counts.get(facet.key);
      for (const value of facet.get(row)) bucket.set(value, (bucket.get(value) || 0) + 1);
    }
  }
  QUICK.forEach((quick, position) => {
    const entries = Object.entries(quick.set);
    quickCounts[position] = state.rows.filter((row) =>
      entries.every(([key, values]) => FACET_BY_KEY.get(key).get(row).some((value) => values.includes(value)))).length;
  });
  const { key, dir } = state.sort;
  const sorters = {
    name: (a, b) => a.name.localeCompare(b.name),
    decision: (a, b) => ["confirm", "yes", "no"].indexOf(a.share) - ["confirm", "yes", "no"].indexOf(b.share) || a.name.localeCompare(b.name),
    relationship: (a, b) => (a.relationship_kind || "~").localeCompare(b.relationship_kind || "~"),
    worth: (a, b) => ["yes", "maybe", "no", ""].indexOf(a.worth) - ["yes", "maybe", "no", ""].indexOf(b.worth),
    last: (a, b) => (a.recency_days ?? 1e9) - (b.recency_days ?? 1e9),
    messages: (a, b) => b.interactions - a.interactions,
  };
  matching.sort((a, b) => dir * sorters[key](a, b));
  state.matching = matching;
  state.counts = counts;
  state.quickCounts = quickCounts;
  if (state.focus >= matching.length) state.focus = matching.length - 1;
}

// ---------- rendering ----------

function renderHead() {
  const rows = state.rows;
  const share = rows.filter((row) => row.share === "yes");
  const confirm = rows.filter((row) => row.share === "confirm").length;
  const priv = rows.filter((row) => row.share === "no").length;
  const withLinkedin = share.filter((row) => row.public_identifier).length;
  els.head.innerHTML = `
    <div class='stat stat-share'><b>${share.length.toLocaleString()}</b><span>share</span></div>
    <div class='stat stat-confirm'><b>${confirm.toLocaleString()}</b><span>confirm</span></div>
    <div class='stat stat-private'><b>${priv.toLocaleString()}</b><span>not sharing</span></div>
    <div class='stat-note'>Upload needs a LinkedIn: <b>${withLinkedin.toLocaleString()}</b> of the ${share.length.toLocaleString()} have one, <b>${(share.length - withLinkedin).toLocaleString()}</b> do not.</div>
    <div class='upload-cmd' title='Runs in your terminal; plans first, uploads with --apply'>
      <code>${escapeHtml(state.uploadCommand)}</code>
      <button type='button' class='btn btn-ghost' data-copy-upload>Copy</button>
    </div>`;
}

function renderQuick() {
  els.quick.innerHTML = QUICK.map((quick, position) => {
    const active = Object.entries(quick.set).every(([key, values]) => {
      const held = state.filters.get(key);
      return held && held.size === values.length && values.every((value) => held.has(value));
    }) && [...state.filters].filter(([, values]) => values.size).length === Object.keys(quick.set).length;
    return `<button type='button' class='chip' data-quick-index='${position}' aria-pressed='${active}'>${escapeHtml(quick.name)} <span class='count'>${state.quickCounts[position].toLocaleString()}</span></button>`;
  }).join("");
}

function renderRail() {
  const facets = FACETS.filter((facet) => !facet.more);
  const more = FACETS.filter((facet) => facet.more);
  const block = (facet) => {
    const held = state.filters.get(facet.key) || new Set();
    const counts = state.counts.get(facet.key) || new Map();
    let values = [...new Set([...counts.keys(), ...held])];
    if (facet.order) values.sort((a, b) => (facet.order.indexOf(a) + 1 || 99) - (facet.order.indexOf(b) + 1 || 99));
    else values.sort((a, b) => (counts.get(b) || 0) - (counts.get(a) || 0) || String(a).localeCompare(String(b)));
    if (facet.search && state.labelSearch) values = values.filter((value) => humanize(value).includes(state.labelSearch));
    const expanded = state.expanded.has(facet.key);
    const shown = expanded || values.length <= VISIBLE_VALUES + 1 ? values : values.slice(0, VISIBLE_VALUES);
    const hidden = values.length - shown.length;
    if (!values.length) return "";
    return `<div class='facet' data-facet='${facet.key}' data-open='${!state.collapsed?.has(facet.key)}'>
      <button type='button' class='facet-head' data-facet-toggle='${facet.key}'>${escapeHtml(facet.label)}${held.size ? " <i class='active-dot' aria-hidden='true'></i>" : ""}</button>
      <div class='facet-body'>
        ${facet.search ? `<input type='search' class='field facet-search' data-label-search value='${escapeHtml(state.labelSearch)}' placeholder='Find a label' aria-label='Find a label'>` : ""}
        ${shown.map((value) => `<button type='button' class='facet-value' data-facet-key='${facet.key}' data-facet-value='${escapeHtml(value)}' aria-pressed='${held.has(value)}' ${!counts.get(value) && !held.has(value) ? "disabled" : ""}><span class='label'>${escapeHtml((facet.text || humanize)(value))}</span><span class='count'>${(counts.get(value) || 0).toLocaleString()}</span></button>`).join("")}
        ${hidden > 0 ? `<button type='button' class='facet-more' data-facet-expand='${facet.key}'>${hidden} more…</button>` : ""}
        ${expanded && values.length > VISIBLE_VALUES + 1 ? `<button type='button' class='facet-more' data-facet-collapse='${facet.key}'>Show fewer</button>` : ""}
      </div></div>`;
  };
  els.rail.innerHTML = facets.map(block).join("")
    + `<button type='button' class='facet-head rail-divider' data-more-toggle aria-expanded='${state.moreOpen}'>More filters</button>`
    + (state.moreOpen ? more.map(block).join("") : "")
    + `<p class='rail-hint'><span class='kbd'>/</span> search · <span class='kbd'>j</span><span class='kbd'>k</span> move · <span class='kbd'>x</span> select · <span class='kbd'>⇧A</span> select all matching · <span class='kbd'>s</span> share · <span class='kbd'>p</span> private · <span class='kbd'>w</span> use worth · <span class='kbd'>z</span> undo · <span class='kbd'>Enter</span> open</p>`;
}

function renderChips() {
  const chips = [];
  for (const [key, values] of state.filters) {
    const facet = FACET_BY_KEY.get(key);
    for (const value of values) {
      chips.push(`<button type='button' class='chip' aria-pressed='true' data-chip-key='${key}' data-chip-value='${escapeHtml(value)}' title='Remove'><em>${escapeHtml(facet.label)}</em> ${escapeHtml((facet.text || humanize)(value))}<span class='x' aria-hidden='true'>×</span></button>`);
    }
  }
  if (chips.length) chips.push("<button type='button' class='btn btn-ghost bar-clear' data-clear-filters>Clear</button>");
  els.chips.innerHTML = chips.join("");
  els.count.textContent = `${state.matching.length.toLocaleString()} of ${state.rows.length.toLocaleString()}`;
}

const COLUMNS = [
  { key: "", label: "" },
  { key: "name", label: "Person" },
  { key: "decision", label: "Decision" },
  { key: "relationship", label: "Relationship" },
  { key: "worth", label: "Worth" },
  { key: "last", label: "Last contact", right: true },
  { key: "messages", label: "Messages", right: true },
];

function renderGridHead() {
  const allSelected = state.matching.length > 0 && state.matching.every((row) => state.selected.has(row.person_id));
  els.gridHead.innerHTML = COLUMNS.map((column, position) => {
    if (position === 0) return `<label class='check'><input type='checkbox' data-select-all aria-label='Select all matching' ${allSelected ? "checked" : ""}></label>`;
    const sort = state.sort.key === column.key ? (state.sort.dir === 1 ? "ascending" : "descending") : "none";
    return `<button type='button' class='${column.right ? "right" : ""}' data-sort='${column.key}' aria-sort='${sort}'>${column.label}</button>`;
  }).join("");
}

function rowHtml(row, position) {
  const selected = state.selected.has(row.person_id);
  const badge = row.share === "yes" ? "badge-ok" : row.share === "confirm" ? "badge-warn" : "badge-muted";
  const who = [row.title, row.company].filter(Boolean).join("<span class='sep'>·</span>") || (row.location ? escapeHtml(row.location) : "");
  const avatar = row.has_avatar ? `<img src='${API}avatar?id=${encodeURIComponent(row.person_id)}' alt='' loading='lazy' referrerpolicy='no-referrer'>` : "";
  const li = row.public_identifier ? `<svg class='li' viewBox='0 0 24 24' aria-label='LinkedIn'><path d='M20.5 2h-17A1.5 1.5 0 002 3.5v17A1.5 1.5 0 003.5 22h17a1.5 1.5 0 001.5-1.5v-17A1.5 1.5 0 0020.5 2zM8 19H5v-9h3zM6.5 8.25A1.75 1.75 0 118.3 6.5a1.78 1.78 0 01-1.8 1.75zM19 19h-3v-4.74c0-1.42-.6-1.93-1.38-1.93A1.74 1.74 0 0013 14.19a.66.66 0 000 .14V19h-3v-9h2.9v1.3a3.11 3.11 0 012.7-1.4c1.55 0 3.36.86 3.36 3.66z'/></svg>` : "";
  const channels = row.channels.map((channel) => `<i data-c='${channel}' title='${channel}'></i>`).join("");
  return `<div class='row' role='row' data-id='${row.person_id}' data-index='${position}' aria-selected='${selected}' data-focus='${position === state.focus}' data-pending='${row.pending ? "true" : "false"}'>
    <label class='check'><input type='checkbox' data-select aria-label='Select ${escapeHtml(row.name)}' ${selected ? "checked" : ""}></label>
    <div class='person'><span class='avatar'>${avatar}<span>${escapeHtml(initials(row.name))}</span></span>
      <span class='who'><b>${escapeHtml(row.name)}${li}<span class='channels'>${channels}</span></b><small>${who ? (row.title || row.company ? `${escapeHtml(row.title)}${row.title && row.company ? "<span class='sep'>·</span>" : ""}${escapeHtml(row.company)}` : who) : ""}</small></span></div>
    <div class='decision'><span class='badge ${badge}'>${label("share", row.share)}</span><small class='${row.share_source === "human" ? "human" : ""}'>${escapeHtml(label("reason", row.reason))}</small></div>
    <div class='rel'>${escapeHtml(humanize(row.relationship_kind))}</div>
    <div class='worth' data-worth='${row.worth}'><i class='dot'></i>${escapeHtml(row.worth || "unjudged")} <span class='src ${row.worth_source}'>${row.worth_source === "human" ? "you" : ""}</span></div>
    <div class='cell-num right ${row.last_interaction ? "" : "dim"}'>${row.last_interaction ? formatDate(row.last_interaction) : "—"}</div>
    <div class='cell-num right ${row.interactions ? "" : "dim"}'>${row.interactions ? row.interactions.toLocaleString() : "—"}</div>
  </div>`;
}

let renderQueued = false;
function renderRows() {
  renderQueued = false;
  const total = state.matching.length;
  els.spacer.style.height = `${total * ROW_H}px`;
  const top = els.viewport.scrollTop;
  const height = els.viewport.clientHeight || 600;
  const start = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
  const end = Math.min(total, Math.ceil((top + height) / ROW_H) + OVERSCAN);
  els.rows.style.transform = `translateY(${start * ROW_H}px)`;
  let html = "";
  for (let position = start; position < end; position += 1) html += rowHtml(state.matching[position], position);
  els.rows.innerHTML = html;
  els.empty.hidden = total > 0;
  if (!total) {
    els.empty.innerHTML = state.rows.length
      ? "No one matches these filters."
      : "No share list yet. Run <code>bin/deep-context share</code> first, then reload.";
  }
}
function scheduleRows() {
  if (renderQueued) return;
  renderQueued = true;
  // A hidden tab gets no animation frames; render on a timer so the page is ready when it shows.
  if (document.visibilityState === "hidden") window.setTimeout(renderRows, 0);
  else window.requestAnimationFrame(renderRows);
}

function renderBulkbar() {
  const count = state.selected.size;
  els.bulkbar.hidden = count === 0 && !state.undo;
  if (els.bulkbar.hidden) return;
  const disabled = state.saving || count === 0 ? "disabled" : "";
  els.bulkbar.innerHTML = `
    <b>${count.toLocaleString()} selected</b>
    <button type='button' class='btn btn-ok' data-action='share' ${disabled}>Share <span class='kbd'>s</span></button>
    <button type='button' class='btn btn-bad' data-action='private' ${disabled}>Keep private <span class='kbd'>p</span></button>
    <button type='button' class='btn' data-action='worth' ${disabled}>Use worth <span class='kbd'>w</span></button>
    <span class='sep'></span>
    ${state.undo ? `<button type='button' class='btn btn-ghost' data-action='undo' ${state.saving ? "disabled" : ""}>Undo <span class='kbd'>z</span></button>` : ""}
    <button type='button' class='btn btn-ghost' data-action='clear' title='Clear selection'>Clear <span class='kbd'>esc</span></button>`;
}

function renderAll({ rows = true } = {}) {
  filterRows();
  renderQuick();
  renderRail();
  renderChips();
  renderGridHead();
  renderBulkbar();
  if (rows) scheduleRows();
  saveFilters();
}

// ---------- filters ----------

function toggleFilter(key, value) {
  const held = state.filters.get(key) || new Set();
  if (held.has(value)) held.delete(value); else held.add(value);
  if (held.size) state.filters.set(key, held); else state.filters.delete(key);
  state.selected.clear();
  state.focus = -1;
  els.viewport.scrollTop = 0;
  renderAll();
}
function setFilters(set) {
  state.filters = new Map(Object.entries(set).map(([key, values]) => [key, new Set(values)]));
  state.selected.clear();
  state.focus = -1;
  els.viewport.scrollTop = 0;
  renderAll();
}

// ---------- selection and writes ----------

function nextTags(row, action) {
  const tags = new Set(row.tags);
  if (action === "share") { tags.delete("private"); tags.add("share"); }
  if (action === "private") { tags.delete("share"); tags.add("private"); }
  if (action === "worth") { tags.delete("share"); tags.delete("private"); }
  return [...tags].sort();
}

async function writeTags(people, { undo }) {
  state.saving = true;
  people.forEach(({ person_id }) => { const row = state.byId.get(person_id); if (row) row.pending = true; });
  renderBulkbar();
  scheduleRows();
  try {
    const response = await fetch(`${API}tags`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ people }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Could not save");
    for (const patch of payload.rows) {
      const row = state.byId.get(patch.person_id);
      if (!row) continue;
      Object.assign(row, { share: patch.share, reason: patch.reason, share_source: patch.share_source, tags: patch.tags });
    }
    state.undo = undo;
    state.selected.clear();
    if (state.drawerId && people.some((entry) => entry.person_id === state.drawerId)) openDrawer(state.drawerId, { refresh: true });
    return payload.rows.length;
  } finally {
    people.forEach(({ person_id }) => { const row = state.byId.get(person_id); if (row) row.pending = false; });
    state.saving = false;
    renderHead();
    renderAll();
  }
}

async function applyAction(action, ids = [...state.selected]) {
  if (state.saving || !ids.length) return;
  const people = ids.map((id) => ({ person_id: id, tags: nextTags(state.byId.get(id), action) }));
  const previous = ids.map((id) => ({ person_id: id, tags: [...state.byId.get(id).tags] }));
  try {
    const count = await writeTags(people, { undo: previous });
    const verb = { share: "Sharing", private: "Keeping private", worth: "Back to worth for" }[action];
    announce(`${verb} ${count.toLocaleString()} ${count === 1 ? "person" : "people"} · z to undo`);
  } catch (error) {
    announce(error.message, true);
  }
}

async function undoLast() {
  if (state.saving || !state.undo) return;
  const previous = state.undo;
  try {
    await writeTags(previous, { undo: null });
    announce(`Undid the last change for ${previous.length.toLocaleString()} ${previous.length === 1 ? "person" : "people"}`);
  } catch (error) {
    announce(error.message, true);
  }
}

function selectAllMatching() {
  const all = state.matching.every((row) => state.selected.has(row.person_id));
  if (all) state.selected.clear();
  else state.matching.forEach((row) => state.selected.add(row.person_id));
  renderGridHead();
  renderBulkbar();
  scheduleRows();
}

function toggleSelected(id) {
  if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
  renderGridHead();
  renderBulkbar();
  scheduleRows();
}

function moveFocus(step) {
  if (!state.matching.length) return;
  state.focus = Math.min(state.matching.length - 1, Math.max(0, state.focus + step));
  const top = state.focus * ROW_H;
  if (top < els.viewport.scrollTop) els.viewport.scrollTop = top;
  else if (top + ROW_H > els.viewport.scrollTop + els.viewport.clientHeight) els.viewport.scrollTop = top + ROW_H - els.viewport.clientHeight;
  scheduleRows();
}

// ---------- drawer ----------

function closeDrawer() {
  state.drawerId = null;
  els.drawer.hidden = true;
  els.drawer.innerHTML = "";
}

async function openDrawer(id, { refresh = false } = {}) {
  const row = state.byId.get(id);
  if (!row) return;
  state.drawerId = id;
  els.drawer.hidden = false;
  if (!refresh) els.drawer.innerHTML = "<div class='drawer-inner'><div class='skeleton' style='height:48px'></div><div class='skeleton' style='height:120px'></div><div class='skeleton' style='height:200px'></div></div>";
  let detail = {};
  try {
    const response = await fetch(`${API}person?id=${encodeURIComponent(id)}`);
    if (response.ok) detail = await response.json();
  } catch { /* the row alone still renders */ }
  if (state.drawerId !== id) return;
  const badge = row.share === "yes" ? "badge-ok" : row.share === "confirm" ? "badge-warn" : "badge-muted";
  const probabilities = Object.entries(detail.probabilities || {}).sort((a, b) => b[1] - a[1]);
  const choices = ["relationship_kind", "mode", "hierarchy", "intro_source", "seniority", "function"];
  const avatar = detail.avatar_url ? `<img src='${escapeHtml(detail.avatar_url)}' alt='' referrerpolicy='no-referrer'>` : "";
  const contact = [...(detail.emails || []), ...(detail.phones || [])];
  els.drawer.innerHTML = `<div class='drawer-inner'>
    <div class='drawer-top'>
      <span class='avatar'>${avatar}<span>${escapeHtml(initials(row.name))}</span></span>
      <div class='who'><h2>${escapeHtml(row.name)}</h2>
        <div class='sub'>${escapeHtml(detail.headline || [row.title, row.company].filter(Boolean).join(" · "))}${row.location ? ` · ${escapeHtml(row.location)}` : ""}</div>
        ${detail.linkedin_url ? `<div class='sub'><a href='${escapeHtml(detail.linkedin_url)}' target='_blank' rel='noreferrer'>${escapeHtml(detail.linkedin_url.replace("https://www.", ""))}</a></div>` : ""}
      </div>
      <button type='button' class='drawer-close' data-drawer-close aria-label='Close'>×</button>
    </div>
    <div class='drawer-actions'>
      <button type='button' class='btn btn-ok' data-one='share' aria-pressed='${row.tags.includes("share")}'>Share</button>
      <button type='button' class='btn btn-bad' data-one='private' aria-pressed='${row.tags.includes("private")}'>Keep private</button>
      <button type='button' class='btn' data-one='worth' aria-pressed='${!row.tags.includes("share") && !row.tags.includes("private")}'>Use worth</button>
    </div>
    <div class='dsec'><h3>Decision</h3>
      <p><span class='badge ${badge}'>${label("share", row.share)}</span> &nbsp;${escapeHtml(label("reason", row.reason))}${row.share_source === "human" ? " · your call" : ""}</p>
      <p class='dim'>Worth ${escapeHtml(row.worth || "unjudged")}${row.worth_source ? ` (${row.worth_source === "human" ? "you" : "model"})` : ""}${detail.worth_reason ? `: ${escapeHtml(detail.worth_reason)}` : ""}</p>
      ${detail.worth_note ? `<p class='note'>${escapeHtml(detail.worth_note)}</p>` : ""}
      ${row.tags.length ? `<div class='tagline'>${row.tags.map((tag) => `<span class='badge badge-info'>${escapeHtml(humanize(tag))}</span>`).join("")}</div>` : ""}
      ${detail.note ? `<p class='note'>${escapeHtml(detail.note)}</p>` : ""}
    </div>
    <div class='dsec'><h3>Contact</h3>
      <dl class='kv'>
        <dt>Channels</dt><dd>${escapeHtml(row.channels.join(", ") || "—")}</dd>
        <dt>Messages</dt><dd><span class='num'>${row.interactions.toLocaleString()}</span>${row.last_interaction ? ` <small>last ${escapeHtml(formatDate(row.last_interaction))}</small>` : ""}</dd>
        ${row.cadence ? `<dt>Cadence</dt><dd>${escapeHtml(row.cadence)}${row.direction ? ` <small>${escapeHtml(humanize(row.direction))}</small>` : ""}</dd>` : ""}
        ${contact.length ? `<dt>Identifiers</dt><dd style='text-transform:none'>${contact.map(escapeHtml).join("<br>")}</dd>` : ""}
      </dl>
    </div>
    ${probabilities.length ? `<div class='dsec'><h3>Labels</h3>
      <dl class='kv'>${choices.filter((key) => row[key]).map((key) => `<dt>${escapeHtml(humanize(key))}</dt><dd>${escapeHtml(humanize(row[key]))} <small>${Math.round(((detail.choice_p || {})[key] || 0) * 100)}%</small></dd>`).join("")}
        ${row.warmth !== null && row.warmth !== undefined ? `<dt>Warmth</dt><dd>${Number(row.warmth).toFixed(1)} / 4 <small>${escapeHtml(row.warmthBucket)}</small></dd>` : ""}</dl>
      <div class='bars'>${probabilities.map(([name, p]) => `<div class='barrow ${p >= .6 ? "active" : ""}'><span class='name'>${escapeHtml(humanize(name))}</span><span class='track'><span class='fill' style='transform:scaleX(${p.toFixed(3)})'></span></span><span class='p'>${Math.round(p * 100)}%</span></div>`).join("")}</div>
    </div>` : `<div class='dsec'><h3>Labels</h3><p class='dim'>No JEV labels: this person has no synthesized context, so only worth decides.</p></div>`}
    ${detail.relationship_to_owner || (detail.topics || []).length || (detail.employers || []).length ? `<div class='dsec'><h3>Facts</h3>
      ${detail.relationship_to_owner ? `<p>${escapeHtml(detail.relationship_to_owner)}</p>` : ""}
      <dl class='kv'>${(detail.employers || []).length ? `<dt>Employers</dt><dd style='text-transform:none'>${detail.employers.map(escapeHtml).join("<br>")}</dd>` : ""}
        ${detail.school ? `<dt>School</dt><dd style='text-transform:none'>${escapeHtml(detail.school)}</dd>` : ""}
        ${(detail.topics || []).length ? `<dt>Topics</dt><dd style='text-transform:none'>${detail.topics.map(escapeHtml).join(", ")}</dd>` : ""}</dl>
    </div>` : ""}
    ${detail.dossier_html ? `<div class='dsec'><h3>Dossier</h3><div class='dossier'>${detail.dossier_html}</div></div>` : ""}
  </div>`;
}

// ---------- events ----------

els.viewport.addEventListener("scroll", scheduleRows, { passive: true });
window.addEventListener("resize", scheduleRows);
// The viewport takes its final height after the first paint; re-render when it does.
new ResizeObserver(scheduleRows).observe(els.viewport);

root.addEventListener("click", async (event) => {
  const target = event.target;
  const facetValue = target.closest("[data-facet-value]");
  if (facetValue) { toggleFilter(facetValue.dataset.facetKey, facetValue.dataset.facetValue); return; }
  const facetToggle = target.closest("[data-facet-toggle]");
  if (facetToggle) {
    state.collapsed ||= new Set();
    const key = facetToggle.dataset.facetToggle;
    if (state.collapsed.has(key)) state.collapsed.delete(key); else state.collapsed.add(key);
    renderRail();
    return;
  }
  const expand = target.closest("[data-facet-expand]");
  if (expand) { state.expanded.add(expand.dataset.facetExpand); renderRail(); return; }
  const collapse = target.closest("[data-facet-collapse]");
  if (collapse) { state.expanded.delete(collapse.dataset.facetCollapse); renderRail(); return; }
  if (target.closest("[data-more-toggle]")) { state.moreOpen = !state.moreOpen; renderRail(); return; }
  const quick = target.closest("[data-quick-index]");
  if (quick) {
    const preset = QUICK[Number(quick.dataset.quickIndex)];
    if (quick.getAttribute("aria-pressed") === "true") setFilters({}); else setFilters(preset.set);
    return;
  }
  const chip = target.closest("[data-chip-key]");
  if (chip) { toggleFilter(chip.dataset.chipKey, chip.dataset.chipValue); return; }
  if (target.closest("[data-clear-filters]")) { setFilters({}); return; }
  if (target.closest("[data-copy-upload]")) {
    await navigator.clipboard.writeText(state.uploadCommand);
    announce("Upload command copied");
    return;
  }
  const sort = target.closest("[data-sort]");
  if (sort) {
    const key = sort.dataset.sort;
    state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : 1 };
    renderAll();
    return;
  }
  if (target.matches("[data-select-all]")) { selectAllMatching(); return; }
  const selectBox = target.closest("[data-select]");
  if (selectBox) { toggleSelected(selectBox.closest(".row").dataset.id); return; }
  const action = target.closest("[data-action]");
  if (action) {
    const name = action.dataset.action;
    if (name === "clear") { state.selected.clear(); renderGridHead(); renderBulkbar(); scheduleRows(); }
    else if (name === "undo") await undoLast();
    else await applyAction(name);
    return;
  }
  const one = target.closest("[data-one]");
  if (one && state.drawerId) { await applyAction(one.dataset.one, [state.drawerId]); return; }
  if (target.closest("[data-drawer-close]")) { closeDrawer(); return; }
  const row = target.closest(".row");
  if (row) {
    state.focus = Number(row.dataset.index);
    scheduleRows();
    openDrawer(row.dataset.id);
  }
});

root.addEventListener("input", (event) => {
  if (event.target === els.search) {
    state.text = els.search.value;
    state.selected.clear();
    state.focus = -1;
    els.viewport.scrollTop = 0;
    renderAll();
  }
  if (event.target.matches("[data-label-search]")) {
    state.labelSearch = event.target.value.toLowerCase();
    const position = event.target.selectionStart;
    renderRail();
    const again = els.rail.querySelector("[data-label-search]");
    again.focus();
    again.setSelectionRange(position, position);
  }
});

document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, select, textarea") || event.metaKey || event.ctrlKey || event.altKey) {
    if (event.key === "Escape") event.target.blur();
    return;
  }
  if (event.repeat && ["s", "p", "w", "z", "x", "Enter"].includes(event.key)) return;
  switch (event.key) {
    case "/": event.preventDefault(); els.search.focus(); els.search.select(); break;
    case "f": event.preventDefault(); els.rail.querySelector(".facet-value")?.focus(); break;
    case "j": case "ArrowDown": event.preventDefault(); moveFocus(1); break;
    case "k": case "ArrowUp": event.preventDefault(); moveFocus(-1); break;
    case "x": if (state.focus >= 0) toggleSelected(state.matching[state.focus].person_id); break;
    case "A": if (event.shiftKey) { event.preventDefault(); selectAllMatching(); } break;
    case "s": if (state.selected.size) void applyAction("share"); break;
    case "p": if (state.selected.size) void applyAction("private"); break;
    case "w": if (state.selected.size) void applyAction("worth"); break;
    case "z": void undoLast(); break;
    case "Enter": if (state.focus >= 0) openDrawer(state.matching[state.focus].person_id); break;
    case "Escape":
      if (state.drawerId) closeDrawer();
      else if (state.selected.size) { state.selected.clear(); renderGridHead(); renderBulkbar(); scheduleRows(); }
      break;
    default: break;
  }
});

// ---------- boot ----------

(async () => {
  els.rows.innerHTML = "<div class='grid-loading'>" + "<div class='skeleton'></div>".repeat(12) + "</div>";
  try {
    await load();
  } catch (error) {
    els.empty.hidden = false;
    els.empty.textContent = error.message;
    els.rows.innerHTML = "";
    return;
  }
  restoreFilters(state.rows.some((row) => row.share === "confirm"));
  els.search.value = state.text;
  renderHead();
  renderAll();
})();
