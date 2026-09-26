// People page: every person once, filtered client-side; bulk share / private tags in one write.
// Vocabulary: person, worth, share / confirm / not sharing, tag, label, flag.
// Rows are keyed DOM nodes: a click, a selection or a focus move patches attributes in
// place; only rows entering or leaving the visible window are built or dropped.

const API = "/api/people/";
const ROW_H = 40;
const OVERSCAN = 8;
const ENTER_ROWS = 14;
const ENTER_STAGGER_MS = 14;
const COUNT_TWEEN_MS = 320;
const FILTERS_KEY = "powerpacks:people-filters:v2";
const YEAR = 365;
const DECISIONS = ["confirm", "yes", "no"];

const root = document.querySelector("[data-people]");
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

// ---------- copy ----------

const BRANDS = { gmail: "Gmail", imessage: "iMessage", whatsapp: "WhatsApp", linkedin: "LinkedIn", jev: "JEV" };
// Plain words for the machine values; anything unmapped falls back to sentence case.
const TEXT = {
  share: { yes: "Sharing", confirm: "Needs confirmation", no: "Not sharing" },
  reason: {
    worth_yes: "Worth: yes", worth_maybe: "Worth: maybe", worth_no: "Worth: no", owner: "You (the owner)",
    human_share: "You chose to share", human_private: "You chose to keep private",
    family: "Family", romantic_partner: "Partner", minor: "Minor", sensitive_context: "Sensitive context",
    sensitive_provider: "Clinician, lawyer, or banker", automated_sender: "Automated sender", stranger: "Stranger",
  },
  worth: { yes: "Yes", maybe: "Maybe", no: "No", unjudged: "Not assessed" },
  source: { human: "You", machine: "AI" },
  mode: { professional_only: "Work only", personal_only: "Personal only", mixed: "Work and personal" },
  hierarchy: { manager: "They managed you", peer: "Same level", report: "You managed them", none: "No reporting line" },
  intro_source: { mutual_friend: "Mutual friend", cold_outreach: "Unsolicited contact" },
  seniority: { mid: "Mid-level" },
  function: { founder_exec: "Founder or executive", people: "People and recruiting" },
  direction: { they_initiate: "Mostly them", i_initiate: "Mostly you", mutual: "Both" },
  cadence: { dormant: "No contact in over 2 years", stale: "No contact in over 1 year" },
  labels: {
    is_coworker_current: "Current colleague", is_coworker_past: "Former colleague", is_vendor_or_partner: "Vendor or partner",
    is_mentor_or_advisor: "Mentor or adviser", is_mentee_or_report: "Someone you mentor or manage",
    is_neighbor_or_local: "Neighbor or local contact", is_healthcare_legal_or_financial_provider: "Healthcare, legal, or financial provider",
    owner_would_intro: "You would introduce them", they_would_take_owner_call: "They would take your call",
    notable: "Publicly notable",
  },
};
const humanize = (value) => String(value ?? "").replace(/^is_/, "").replaceAll("_", " ").trim();
// Sentence case, brand names kept: "close friend" -> "Close friend", "imessage" -> "iMessage".
function sentence(value) {
  const words = humanize(value).split(" ").filter(Boolean);
  if (!words.length) return "";
  const cased = words.map((word) => BRANDS[word.toLowerCase()] || word);
  if (!BRANDS[words[0].toLowerCase()]) cased[0] = cased[0][0].toUpperCase() + cased[0].slice(1);
  return cased.join(" ");
}
const label = (kind, value) => (TEXT[kind] && TEXT[kind][value]) || sentence(value);
const plural = (count, noun) => `${count.toLocaleString()} ${count === 1 ? noun : (noun === "person" ? "people" : `${noun}s`)}`;
const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");

// One icon per source family, the same glyphs the search viewer uses.
const CHANNEL = {
  gmail: { title: "Gmail", path: "<rect width='20' height='16' x='2' y='4' rx='2'/><path d='m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7'/>" },
  imessage: { title: "iMessage", path: "<path d='M7.9 20A9 9 0 1 0 4 16.1L2 22Z'/>" },
  whatsapp: { title: "WhatsApp", path: "<path d='M7.9 20A9 9 0 1 0 4 16.1L2 22Z'/><path d='M9 10a3 3 0 0 0 6 4'/>" },
  linkedin: { title: "LinkedIn", fill: true, path: "<path d='M20.5 2h-17A1.5 1.5 0 002 3.5v17A1.5 1.5 0 003.5 22h17a1.5 1.5 0 001.5-1.5v-17A1.5 1.5 0 0020.5 2zM8 19H5v-9h3zM6.5 8.25A1.75 1.75 0 118.3 6.5a1.78 1.78 0 01-1.8 1.75zM19 19h-3v-4.74c0-1.42-.6-1.93-1.38-1.93A1.74 1.74 0 0013 14.19a.66.66 0 000 .14V19h-3v-9h2.9v1.3a3.11 3.11 0 012.7-1.4c1.55 0 3.36.86 3.36 3.66z'/>" },
};
function channelIcon(channel) {
  const spec = CHANNEL[channel];
  if (!spec) return "";
  const attrs = spec.fill ? "fill='currentColor' stroke='none'" : "fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'";
  return `<span class='source' data-c='${channel}' title='${spec.title}'><svg viewBox='0 0 24 24' ${attrs} aria-label='${spec.title}'>${spec.path}</svg></span>`;
}

function warmthCell(value) {
  if (value === null || value === undefined) return "<div class='warmth-cell c-warmth dim'>—</div>";
  const on = Math.round(Number(value));
  const bars = [1, 2, 3, 4].map((level) => `<i class='${level <= on ? "on" : ""}'></i>`).join("");
  return `<div class='warmth-cell c-warmth' title='Warmth ${Number(value).toFixed(1)} of 4'><span class='warmth-bar'>${bars}</span>${Number(value).toFixed(1)}</div>`;
}

const LAST = ["< 1 year", "1–2 years", "> 2 years", "Never"];
function lastBucket(days) {
  if (days === null || days === undefined) return LAST[3];
  if (days < YEAR) return LAST[0];
  if (days <= 2 * YEAR) return LAST[1];
  return LAST[2];
}
const WARMTH = ["Distant (0–1)", "Friendly (1–2)", "Close (2–3)", "Inner circle (3–4)"];
function warmthBucket(value) {
  if (value === null || value === undefined) return "";
  return WARMTH[Math.min(3, Math.floor(value))];
}

// Facets: OR within one, AND across, always within the selected decision tab.
// `get` returns the row's values for the facet; `text` renders one value. The
// default facets stay open; the rest collapse under "More filters".
const FACETS = [
  { key: "flag", label: "Flag", get: (r) => (r.flag ? [r.flag] : []), words: "reason" },
  { key: "worth", label: "Worth", get: (r) => [r.worth || "unjudged"], order: ["yes", "maybe", "no", "unjudged"] },
  { key: "relationship_kind", label: "Relationship", get: (r) => (r.relationship_kind ? [r.relationship_kind] : []) },
  { key: "last", label: "Last contact", get: (r) => [r.last], order: LAST },
  { key: "channels", label: "Sources", get: (r) => r.channels, order: ["gmail", "imessage", "whatsapp", "linkedin"] },
  { key: "linkedin", label: "LinkedIn", get: (r) => [r.public_identifier ? "Has LinkedIn" : "No LinkedIn"], order: ["Has LinkedIn", "No LinkedIn"] },
  { key: "reason", label: "Reason", get: (r) => [r.reason], more: true },
  { key: "worth_source", label: "Worth decided by", get: (r) => (r.worth_source ? [r.worth_source] : []), words: "source", more: true },
  { key: "tags", label: "Your tags", get: (r) => r.tags, more: true },
  { key: "labels", label: "Relationship labels", get: (r) => r.labels, more: true, search: true },
  { key: "function", label: "Function", get: (r) => (r.function ? [r.function] : []), more: true },
  { key: "seniority", label: "Seniority", get: (r) => (r.seniority ? [r.seniority] : []), more: true },
  { key: "mode", label: "Conversation", get: (r) => (r.mode ? [r.mode] : []), more: true },
  { key: "warmth", label: "Warmth", get: (r) => (r.warmthBucket ? [r.warmthBucket] : []), order: WARMTH, more: true },
  { key: "direction", label: "Who writes", get: (r) => (r.direction ? [r.direction] : []), more: true },
  { key: "hierarchy", label: "Reporting relationship", get: (r) => (r.hierarchy ? [r.hierarchy] : []), more: true },
  { key: "intro_source", label: "How you met", get: (r) => (r.intro_source ? [r.intro_source] : []), more: true },
  { key: "evidence", label: "Evidence", get: (r) => [
    ...(r.linkedin_only ? ["No relationship labels"] : []), ...(r.group_chat_only ? ["Group chats only"] : []),
    ...(r.shared_employer ? ["Shared employer"] : []), ...(r.shared_school ? ["Shared school"] : []),
  ], more: true },
];
const FACET_BY_KEY = new Map(FACETS.map((facet) => [facet.key, facet]));
const facetText = (facet, value) => label(facet.words || facet.key, value);
const VISIBLE_VALUES = 8;

// Quick filters: named facet selections with explicit predicates, counted within the tab.
const QUICK = [
  { name: "Family", set: { flag: ["family"] } },
  { name: "Sensitive context", set: { flag: ["sensitive_context"] } },
  { name: "Service providers", set: { relationship_kind: ["service_provider"] } },
  { name: "Recruiters", set: { labels: ["is_recruiter"] } },
  { name: "Strangers or automated senders", set: { flag: ["stranger", "automated_sender"] } },
  { name: "Last contact > 2 years", set: { last: [LAST[2]] } },
  { name: "Close friends", set: { relationship_kind: ["close_friend"] } },
];

const state = {
  rows: [], byId: new Map(),
  tab: "confirm", filters: new Map(), text: "", sort: { key: "name", dir: 1 },
  matching: [], counts: new Map(), quickCounts: [], selected: new Set(), focus: -1,
  drawerId: null, undo: null, saving: false, expanded: new Set(), collapsed: new Set(), moreOpen: false, labelSearch: "",
  hintOpen: false,
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
function setAttr(node, name, value) {
  const text = String(value);
  if (node.getAttribute(name) !== text) node.setAttribute(name, text);
}
// Counts roll to their new value instead of jumping.
function tweenCount(node, to) {
  const from = Number(node.dataset.value ?? to);
  node.dataset.value = String(to);
  if (from === to || document.visibilityState === "hidden" || REDUCED_MOTION.matches) { node.textContent = to.toLocaleString(); return; }
  const startedAt = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - startedAt) / COUNT_TWEEN_MS);
    const eased = 1 - (1 - t) ** 3;
    node.textContent = Math.round(from + (to - from) * eased).toLocaleString();
    if (t < 1) window.requestAnimationFrame(step);
  };
  window.requestAnimationFrame(step);
}
function announce(message, { error = false, undo = false } = {}) {
  els.toast.innerHTML = `<span>${escapeHtml(message)}</span>`
    + (undo ? "<button type='button' data-action='undo'>Undo <span class='kbd'>Z</span></button>" : "");
  els.toast.classList.toggle("error", error);
  els.toast.classList.add("show");
  window.clearTimeout(announce.timer);
  announce.timer = window.setTimeout(() => els.toast.classList.remove("show"), error ? 8000 : 6000);
}
function saveFilters() {
  const filters = Object.fromEntries([...state.filters].map(([key, values]) => [key, [...values]]));
  try { sessionStorage.setItem(FILTERS_KEY, JSON.stringify({ tab: state.tab, filters, text: state.text, sort: state.sort })); } catch { /* fine */ }
}
function restoreFilters() {
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(FILTERS_KEY) || "null"); } catch { /* fresh */ }
  if (saved && saved.filters) {
    state.filters = new Map(Object.entries(saved.filters).filter(([key]) => FACET_BY_KEY.has(key))
      .map(([key, values]) => [key, new Set(values)]));
    state.text = saved.text || "";
    state.sort = saved.sort || state.sort;
    if (DECISIONS.includes(saved.tab)) state.tab = saved.tab;
  }
  // Start where the human is needed; fall back to what is shared, then to the rest.
  if (!state.rows.some((row) => row.share === state.tab)) {
    state.tab = DECISIONS.find((decision) => state.rows.some((row) => row.share === decision)) || "confirm";
  }
}

// ---------- data ----------

async function load() {
  const response = await fetch(`${API}rows`);
  if (!response.ok) throw new Error((await response.text()) || "Couldn't load people.");
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
}

function filterRows() {
  const needle = state.text.trim().toLowerCase();
  const active = [...state.filters].filter(([, values]) => values.size);
  const counts = new Map(FACETS.map((facet) => [facet.key, new Map()]));
  const quickCounts = QUICK.map(() => 0);
  const matching = [];
  for (const row of state.rows) {
    if (row.share !== state.tab) continue;
    QUICK.forEach((quick, position) => {
      if (Object.entries(quick.set).every(([key, values]) => FACET_BY_KEY.get(key).get(row).some((value) => values.includes(value)))) {
        quickCounts[position] += 1;
      }
    });
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
  const { key, dir } = state.sort;
  const sorters = {
    name: (a, b) => a.name.localeCompare(b.name),
    reason: (a, b) => a.reason.localeCompare(b.reason) || a.name.localeCompare(b.name),
    relationship: (a, b) => (a.relationship_kind || "~").localeCompare(b.relationship_kind || "~"),
    worth: (a, b) => ["yes", "maybe", "no", ""].indexOf(a.worth) - ["yes", "maybe", "no", ""].indexOf(b.worth),
    warmth: (a, b) => (b.warmth ?? -1) - (a.warmth ?? -1),
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

// The three decision counts are the tabs. Built once; counts roll and the ink slides.
function renderHead() {
  const totals = Object.fromEntries(DECISIONS.map((decision) => [decision, state.rows.filter((row) => row.share === decision).length]));
  if (!els.head.childElementCount) {
    els.head.innerHTML = DECISIONS.map((decision) => `
      <button type='button' class='stat stat-${decision}' data-tab='${decision}' aria-pressed='false'>
        <b>0</b><span>${label("share", decision)}</span></button>`).join("")
      + "<i class='tab-ink' aria-hidden='true'></i><span class='head-note'></span>";
  }
  for (const decision of DECISIONS) {
    const tab = els.head.querySelector(`[data-tab='${decision}']`);
    setAttr(tab, "aria-pressed", state.tab === decision);
    tweenCount(tab.querySelector("b"), totals[decision]);
  }
  els.head.querySelector(".head-note").textContent = plural(state.rows.length, "person");
  moveInk();
}
function moveInk() {
  const active = els.head.querySelector("[data-tab][aria-pressed='true']");
  const ink = els.head.querySelector(".tab-ink");
  if (!active || !ink) return;
  const first = !ink.dataset.placed;
  if (first) ink.style.transition = "none";
  ink.style.transform = `translateX(${active.offsetLeft}px)`;
  ink.style.width = `${active.offsetWidth}px`;
  if (first) {
    ink.dataset.placed = "1";
    window.requestAnimationFrame(() => { ink.style.transition = ""; });
  }
}

function renderQuick() {
  els.quick.innerHTML = QUICK.map((quick, position) => {
    const active = Object.entries(quick.set).every(([key, values]) => {
      const held = state.filters.get(key);
      return held && held.size === values.length && values.every((value) => held.has(value));
    }) && [...state.filters].filter(([, values]) => values.size).length === Object.keys(quick.set).length;
    return `<button type='button' class='chip' data-quick-index='${position}' aria-pressed='${active}' ${state.quickCounts[position] || active ? "" : "disabled"}>${escapeHtml(quick.name)} <span class='count'>${state.quickCounts[position].toLocaleString()}</span></button>`;
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
    if (facet.search && state.labelSearch) values = values.filter((value) => facetText(facet, value).toLowerCase().includes(state.labelSearch));
    const expanded = state.expanded.has(facet.key);
    const shown = expanded || values.length <= VISIBLE_VALUES + 1 ? values : values.slice(0, VISIBLE_VALUES);
    const hidden = values.length - shown.length;
    if (!values.length) return "";
    return `<div class='facet' data-facet='${facet.key}' data-open='${!state.collapsed.has(facet.key)}'>
      <button type='button' class='facet-head' data-facet-toggle='${facet.key}' aria-expanded='${!state.collapsed.has(facet.key)}'>${escapeHtml(facet.label)}${held.size ? " <i class='active-dot' aria-hidden='true'></i>" : ""}</button>
      <div class='facet-body'><div class='facet-list'>
        ${facet.search ? `<input type='search' class='field facet-search' data-label-search value='${escapeHtml(state.labelSearch)}' placeholder='Find a label' aria-label='Find a label'>` : ""}
        ${shown.map((value) => `<button type='button' class='facet-value' data-facet-key='${facet.key}' data-facet-value='${escapeHtml(value)}' aria-pressed='${held.has(value)}' ${!counts.get(value) && !held.has(value) ? "disabled" : ""}><span class='label'>${escapeHtml(facetText(facet, value))}</span><span class='count'>${(counts.get(value) || 0).toLocaleString()}</span></button>`).join("")}
        ${hidden > 0 ? `<button type='button' class='facet-more' data-facet-expand='${facet.key}'>${hidden} more…</button>` : ""}
        ${expanded && values.length > VISIBLE_VALUES + 1 ? `<button type='button' class='facet-more' data-facet-collapse='${facet.key}'>Show fewer</button>` : ""}
      </div></div></div>`;
  };
  els.rail.innerHTML = facets.map(block).join("")
    + `<button type='button' class='facet-head rail-divider' data-more-toggle aria-expanded='${state.moreOpen}'>More filters</button>`
    + (state.moreOpen ? more.map(block).join("") : "")
    + `<details class='rail-hint' ${state.hintOpen ? "open" : ""} data-hint><summary>Keyboard shortcuts</summary><dl>
        <dt><span class='kbd'>1</span><span class='kbd'>2</span><span class='kbd'>3</span></dt><dd>Switch tab</dd>
        <dt><span class='kbd'>/</span></dt><dd>Search</dd>
        <dt><span class='kbd'>J</span><span class='kbd'>K</span></dt><dd>Move</dd>
        <dt><span class='kbd'>X</span></dt><dd>Select</dd>
        <dt><span class='kbd'>⇧A</span></dt><dd>Select all matching</dd>
        <dt><span class='kbd'>S</span></dt><dd>Share</dd>
        <dt><span class='kbd'>P</span></dt><dd>Keep private</dd>
        <dt><span class='kbd'>W</span></dt><dd>Use worth</dd>
        <dt><span class='kbd'>Z</span></dt><dd>Undo</dd>
        <dt><span class='kbd'>Enter</span></dt><dd>Open or close details</dd>
      </dl></details>`;
}

function renderChips() {
  const chips = [];
  for (const [key, values] of state.filters) {
    const facet = FACET_BY_KEY.get(key);
    for (const value of values) {
      chips.push(`<button type='button' class='chip' aria-pressed='true' data-chip-key='${key}' data-chip-value='${escapeHtml(value)}' title='Remove'><em>${escapeHtml(facet.label)}</em> ${escapeHtml(facetText(facet, value))}<span class='x' aria-hidden='true'>×</span></button>`);
    }
  }
  if (chips.length) chips.push("<button type='button' class='btn btn-ghost bar-clear' data-clear-filters>Clear filters</button>");
  els.chips.innerHTML = chips.join("");
  const inTab = state.rows.filter((row) => row.share === state.tab).length;
  const shown = state.matching.length;
  els.count.textContent = shown === inTab ? plural(inTab, "person") : `${shown.toLocaleString()} of ${inTab.toLocaleString()} people`;
}

const COLUMNS = [
  { key: "", cls: "c-check", label: "" },
  { key: "name", cls: "c-person", label: "Person" },
  { key: "sources", cls: "c-sources", label: "Sources", sortable: false },
  { key: "reason", cls: "c-why", label: "Reason" },
  { key: "relationship", cls: "c-rel", label: "Relationship" },
  { key: "worth", cls: "c-worth", label: "Worth" },
  { key: "warmth", cls: "c-warmth", label: "Warmth" },
  { key: "last", cls: "c-last", label: "Last contact", right: true },
  { key: "messages", cls: "c-msgs", label: "Interactions", right: true },
];

function renderGridHead() {
  const selectedHere = state.matching.filter((row) => state.selected.has(row.person_id)).length;
  const allSelected = state.matching.length > 0 && selectedHere === state.matching.length;
  els.gridHead.innerHTML = COLUMNS.map((column, position) => {
    if (position === 0) return `<label class='check c-check'><input type='checkbox' data-select-all aria-label='Select all ${state.matching.length.toLocaleString()} matching people' ${allSelected ? "checked" : ""}></label>`;
    if (column.sortable === false) return `<span class='${column.cls}'>${column.label}</span>`;
    const sort = state.sort.key === column.key ? (state.sort.dir === 1 ? "ascending" : "descending") : "none";
    return `<button type='button' class='${column.cls} ${column.right ? "right" : ""}' data-sort='${column.key}' aria-sort='${sort}'>${column.label}</button>`;
  }).join("");
  els.gridHead.querySelector("[data-select-all]").indeterminate = selectedHere > 0 && !allSelected;
}

function rowCells(row) {
  const sub = row.title || row.company
    ? `${escapeHtml(row.title)}${row.title && row.company ? "<span class='sep'>·</span>" : ""}${escapeHtml(row.company)}`
    : escapeHtml(row.location);
  const avatar = row.has_avatar ? `<img src='${API}avatar?id=${encodeURIComponent(row.person_id)}' alt='' loading='lazy' referrerpolicy='no-referrer'>` : "";
  return `<label class='check c-check'><input type='checkbox' data-select aria-label='Select ${escapeHtml(row.name)}'></label>
    <div class='person c-person'><span class='avatar'>${avatar}<span>${escapeHtml(initials(row.name))}</span></span>
      <span class='who'><b>${escapeHtml(row.name)}</b><small>${sub}</small></span></div>
    <div class='sources c-sources'>${row.channels.map(channelIcon).join("")}</div>
    <div class='why c-why ${row.share_source === "human" ? "human" : ""}'>${escapeHtml(label("reason", row.reason))}</div>
    <div class='rel c-rel'>${escapeHtml(sentence(row.relationship_kind))}</div>
    <div class='worth c-worth' data-worth='${row.worth}'><i class='dot'></i>${escapeHtml(label("worth", row.worth || "unjudged"))} <span class='src ${row.worth_source}'>${row.worth_source === "human" ? "You" : ""}</span></div>
    ${warmthCell(row.warmth)}
    <div class='cell-num c-last right ${row.last_interaction ? "" : "dim"}'>${row.last_interaction ? formatDate(row.last_interaction) : "—"}</div>
    <div class='cell-num c-msgs right ${row.interactions ? "" : "dim"}'>${row.interactions ? row.interactions.toLocaleString() : "—"}</div>`;
}

const rowNodes = new Map();
function buildRow(row) {
  const node = document.createElement("div");
  node.className = "row";
  node.setAttribute("role", "row");
  node.dataset.id = row.person_id;
  node.innerHTML = rowCells(row);
  return node;
}
function syncRow(node, row, position) {
  const selected = state.selected.has(row.person_id);
  setAttr(node, "data-index", position);
  setAttr(node, "aria-selected", selected);
  setAttr(node, "data-focus", position === state.focus);
  setAttr(node, "data-open", row.person_id === state.drawerId);
  setAttr(node, "data-pending", Boolean(row.pending));
  const box = node.querySelector("[data-select]");
  if (box.checked !== selected) box.checked = selected;
}
function refreshRow(id) {
  const node = rowNodes.get(id);
  if (node) node.innerHTML = rowCells(state.byId.get(id));
}

let renderQueued = false;
let enterRows = false;
function renderRows() {
  renderQueued = false;
  const total = state.matching.length;
  els.spacer.style.height = `${total * ROW_H}px`;
  const top = els.viewport.scrollTop;
  const height = els.viewport.clientHeight || 600;
  const start = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
  const end = Math.min(total, Math.ceil((top + height) / ROW_H) + OVERSCAN);
  els.rows.style.transform = `translateY(${start * ROW_H}px)`;
  let cursor = els.rows.firstElementChild;
  let entered = 0;
  for (let position = start; position < end; position += 1) {
    const row = state.matching[position];
    let node = rowNodes.get(row.person_id);
    if (!node) {
      node = buildRow(row);
      rowNodes.set(row.person_id, node);
      if (enterRows && entered < ENTER_ROWS) {
        node.classList.add("row-enter");
        node.style.animationDelay = `${entered * ENTER_STAGGER_MS}ms`;
        entered += 1;
      }
    }
    syncRow(node, row, position);
    if (node === cursor) cursor = cursor.nextElementSibling;
    else els.rows.insertBefore(node, cursor);
  }
  while (cursor) {
    const stale = cursor;
    cursor = cursor.nextElementSibling;
    if (rowNodes.get(stale.dataset.id) === stale) rowNodes.delete(stale.dataset.id);
    stale.remove();
  }
  enterRows = false;
  els.empty.hidden = total > 0;
  if (!total) els.empty.innerHTML = emptyText();
}
function emptyText() {
  if (!state.rows.length) return "No people to review yet. Run <code>bin/deep-context share</code>, then reload.";
  if (state.filters.size || state.text) return "No people match these filters. <button type='button' class='btn btn-ghost' data-clear-filters>Clear filters</button>";
  return { confirm: "No one needs confirmation.", yes: "No people marked for sharing.", no: "No people marked as not sharing." }[state.tab];
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
  els.bulkbar.dataset.open = String(count > 0);
  els.bulkbar.inert = count === 0;
  if (!count) return;
  const disabled = state.saving ? "disabled" : "";
  els.bulkbar.innerHTML = `
    <b>${count.toLocaleString()} selected</b>
    <button type='button' class='btn btn-ok' data-action='share' ${disabled}>Share <span class='kbd'>S</span></button>
    <button type='button' class='btn' data-action='private' ${disabled}>Keep private <span class='kbd'>P</span></button>
    <button type='button' class='btn btn-ghost' data-action='worth' ${disabled} title='Removes your choice; worth and flags decide'>Use worth <span class='kbd'>W</span></button>
    <span class='sep'></span>
    <button type='button' class='btn btn-ghost' data-action='clear'>Clear selection <span class='kbd'>Esc</span></button>`;
}

function renderAll({ rows = true } = {}) {
  filterRows();
  renderHead();
  renderQuick();
  renderRail();
  renderChips();
  renderGridHead();
  renderBulkbar();
  if (rows) scheduleRows();
  saveFilters();
}

// ---------- filters ----------

function resetView() {
  state.selected.clear();
  state.focus = -1;
  els.viewport.scrollTop = 0;
  enterRows = true;
  renderAll();
}
function toggleFilter(key, value) {
  const held = state.filters.get(key) || new Set();
  if (held.has(value)) held.delete(value); else held.add(value);
  if (held.size) state.filters.set(key, held); else state.filters.delete(key);
  resetView();
}
function setFilters(set) {
  state.filters = new Map(Object.entries(set).map(([key, values]) => [key, new Set(values)]));
  resetView();
}
function setTab(decision) {
  if (!DECISIONS.includes(decision) || decision === state.tab) return;
  state.tab = decision;
  resetView();
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
    if (!response.ok) throw new Error(payload.error || "Try again.");
    for (const patch of payload.rows) {
      const row = state.byId.get(patch.person_id);
      if (!row) continue;
      Object.assign(row, { share: patch.share, reason: patch.reason, share_source: patch.share_source, tags: patch.tags });
      refreshRow(patch.person_id);
    }
    state.undo = undo;
    state.selected.clear();
    if (state.drawerId && people.some((entry) => entry.person_id === state.drawerId)) openDrawer(state.drawerId, { refresh: true });
    return payload.rows.length;
  } finally {
    people.forEach(({ person_id }) => { const row = state.byId.get(person_id); if (row) row.pending = false; });
    state.saving = false;
    renderAll();
  }
}

async function applyAction(action, ids = [...state.selected]) {
  if (state.saving || !ids.length) return;
  const people = ids.map((id) => ({ person_id: id, tags: nextTags(state.byId.get(id), action) }));
  const previous = ids.map((id) => ({ person_id: id, tags: [...state.byId.get(id).tags] }));
  try {
    const count = await writeTags(people, { undo: previous });
    announce({
      share: `Marked ${plural(count, "person")} for sharing.`,
      private: `Marked ${plural(count, "person")} private.`,
      worth: `Removed your sharing choice for ${plural(count, "person")}.`,
    }[action], { undo: true });
  } catch (error) {
    announce(`Couldn't save changes. ${error.message}`, { error: true });
  }
}

async function undoLast() {
  if (state.saving || !state.undo) return;
  const previous = state.undo;
  try {
    await writeTags(previous, { undo: null });
    announce(`Undid changes for ${plural(previous.length, "person")}.`);
  } catch (error) {
    announce(`Couldn't undo. ${error.message}`, { error: true });
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

function clearSelection() {
  state.selected.clear();
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

let detailRequest = null;
let swapStartedAt = 0;

function closeDrawer() {
  detailRequest?.abort();
  state.drawerId = null;
  root.dataset.drawerOpen = "false";
  els.drawer.setAttribute("aria-hidden", "true");
  els.drawer.inert = true;
  scheduleRows();
}
function toggleDrawer(id) {
  if (state.drawerId === id) closeDrawer(); else void openDrawer(id);
}

// The row renders at once; the detail (labels, facts, dossier, contact points) follows.
async function openDrawer(id, { refresh = false } = {}) {
  const row = state.byId.get(id);
  if (!row) return;
  const swapping = Boolean(state.drawerId) && state.drawerId !== id;
  state.drawerId = id;
  root.dataset.drawerOpen = "true";
  els.drawer.inert = false;
  els.drawer.removeAttribute("aria-hidden");
  scheduleRows();
  detailRequest?.abort();
  detailRequest = new AbortController();
  if (swapping) swapStartedAt = performance.now();
  if (!refresh) {
    renderDrawer(row, null);
    els.drawer.scrollTop = 0;
  }
  let detail;
  try {
    const response = await fetch(`${API}person?id=${encodeURIComponent(id)}`, { signal: detailRequest.signal });
    detail = response.ok ? await response.json() : {};
  } catch (error) {
    if (error.name === "AbortError") return;
    detail = { failed: true };
  }
  if (state.drawerId !== id) return;
  const scroll = els.drawer.scrollTop;
  renderDrawer(row, detail);
  els.drawer.scrollTop = scroll;
}

const CHOICES = ["relationship_kind", "mode", "hierarchy", "intro_source", "seniority", "function"];
const dt = (key, value) => (value ? `<dt>${escapeHtml(key)}</dt><dd>${value}</dd>` : "");
const lines = (values) => values.map(escapeHtml).join("<br>");

// A person switch fades the new content in; a re-render inside that fade continues it.
function swapStyle() {
  const elapsed = performance.now() - swapStartedAt;
  return elapsed < 200 ? ` class='drawer-inner swap' style='animation-delay:-${Math.round(elapsed)}ms'` : " class='drawer-inner'";
}

function renderDrawer(row, detail) {
  const badge = row.share === "yes" ? "badge-ok" : row.share === "confirm" ? "badge-warn" : "badge-muted";
  const avatar = detail?.avatar_url ? `<img src='${escapeHtml(detail.avatar_url)}' alt='' referrerpolicy='no-referrer'>`
    : row.has_avatar ? `<img src='${API}avatar?id=${encodeURIComponent(row.person_id)}' alt='' referrerpolicy='no-referrer'>` : "";
  const headline = detail?.headline || [row.title, row.company].filter(Boolean).join(" · ");
  const shares = row.tags.includes("share");
  const keepsPrivate = row.tags.includes("private");
  const worthLine = `Worth: ${escapeHtml(label("worth", row.worth || "unjudged").toLowerCase())}`
    + (row.worth_source ? ` · Decided by ${escapeHtml(label("source", row.worth_source).toLowerCase() === "ai" ? "AI" : "you")}` : "");
  const head = `
    <div class='drawer-top'>
      <span class='avatar'>${avatar}<span>${escapeHtml(initials(row.name))}</span></span>
      <div class='who'><h2>${escapeHtml(row.name)}</h2>
        ${headline ? `<div class='sub'>${escapeHtml(headline)}</div>` : ""}
        ${row.location ? `<div class='sub'>${escapeHtml(row.location)}</div>` : ""}
        <div class='sub sources'>${row.channels.map(channelIcon).join("")}${detail?.linkedin_url ? `<a href='${escapeHtml(detail.linkedin_url)}' target='_blank' rel='noreferrer'>View LinkedIn profile</a>` : ""}</div>
      </div>
      <button type='button' class='drawer-close' data-drawer-close aria-label='Close details'>×</button>
    </div>
    <div class='drawer-actions' ${state.saving ? "data-saving" : ""}>
      <button type='button' class='btn ${shares ? "btn-ok" : ""}' data-one='share' aria-pressed='${shares}' ${state.saving ? "disabled" : ""}>${shares ? "✓ " : ""}Share</button>
      <button type='button' class='btn' data-one='private' aria-pressed='${keepsPrivate}' ${state.saving ? "disabled" : ""}>${keepsPrivate ? "✓ " : ""}Keep private</button>
    </div>
    <div class='drawer-worth'>
      <button type='button' class='btn btn-ghost' data-one='worth' aria-pressed='${!shares && !keepsPrivate}' ${state.saving || (!shares && !keepsPrivate) ? "disabled" : ""}>Use worth</button>
      <span>${state.saving ? "Saving…" : "Removes your choice; worth and flags decide."}</span>
    </div>
    <div class='dsec'><h3>Decision</h3>
      <p><span class='badge ${badge}'>${label("share", row.share)}</span> &nbsp;${escapeHtml(label("reason", row.reason))}</p>
      <p class='dim'>${worthLine}${detail?.worth_reason ? `: ${escapeHtml(detail.worth_reason)}` : ""}</p>
      ${detail?.worth_note ? `<p class='note'>${escapeHtml(detail.worth_note)}</p>` : ""}
      ${detail?.note ? `<p class='note'>${escapeHtml(detail.note)}</p>` : ""}
    </div>`;
  if (detail === null) {
    els.drawer.innerHTML = `<div${swapStyle()}>${head}<div class='dsec' aria-busy='true'><h3>Details</h3><p class='dim'>Loading details…</p><div class='skeleton' style='height:12px;width:70%'></div><div class='skeleton' style='height:12px;width:50%'></div></div></div>`;
    return;
  }
  if (detail.failed) {
    els.drawer.innerHTML = `<div${swapStyle()}>${head}<div class='dsec'><h3>Details</h3><p class='dim'>Couldn't load details. <button type='button' class='btn btn-ghost' data-drawer-retry>Retry</button></p></div></div>`;
    return;
  }
  const probabilities = Object.entries(detail.probabilities || {}).sort((a, b) => b[1] - a[1]);
  const contact = [...(detail.emails || []), ...(detail.phones || [])];
  const relationship = probabilities.length ? `<div class='dsec'><h3>Relationship</h3>
      <dl class='kv'>${CHOICES.filter((key) => row[key]).map((key) => dt(FACET_BY_KEY.get(key).label,
        `${escapeHtml(facetText(FACET_BY_KEY.get(key), row[key]))} <small>${Math.round(((detail.choice_p || {})[key] || 0) * 100)}%</small>`)).join("")}
        ${row.warmth !== null && row.warmth !== undefined ? dt("Warmth", `${Number(row.warmth).toFixed(1)} of 4 <small>${escapeHtml(row.warmthBucket)}</small>`) : ""}</dl>
    </div>` : "<div class='dsec'><h3>Relationship</h3><p class='dim'>No relationship labels available.</p></div>";
  const facts = detail.relationship_to_owner || (detail.topics || []).length || (detail.employers || []).length ? `<div class='dsec'><h3>Facts</h3>
      ${detail.relationship_to_owner ? `<p>${escapeHtml(detail.relationship_to_owner)}</p>` : ""}
      <dl class='kv'>${dt("Employers", (detail.employers || []).length ? lines(detail.employers) : "")}
        ${dt("School", detail.school ? escapeHtml(detail.school) : "")}
        ${dt("Topics", (detail.topics || []).length ? escapeHtml(detail.topics.join(", ")) : "")}</dl>
    </div>` : "";
  const dossier = detail.dossier_html ? `<div class='dsec'><h3>Dossier</h3><div class='dossier'>${detail.dossier_html}</div></div>` : "";
  const contactSection = `<div class='dsec'><h3>Contact</h3>
      <dl class='kv'>
        ${dt("Interactions", `<span class='num'>${row.interactions.toLocaleString()}</span>`)}
        ${dt("Last contact", row.last_interaction ? escapeHtml(formatDate(row.last_interaction)) : "")}
        ${dt("Contact frequency", row.cadence ? `${escapeHtml(label("cadence", row.cadence))}${row.direction ? ` <small>writes: ${escapeHtml(label("direction", row.direction).toLowerCase())}</small>` : ""}` : "")}
        ${dt("Email and phone", contact.length ? lines(contact) : "")}
      </dl>
    </div>`;
  const confidence = probabilities.length ? `<details class='dsec'><summary><h3>Label confidence</h3></summary>
      <div class='bars'>${probabilities.map(([name, p]) => `<div class='barrow ${p >= .6 ? "active" : ""}'><span class='name'>${escapeHtml(label("labels", name))}</span><span class='track'><span class='fill' style='transform:scaleX(${p.toFixed(3)})'></span></span><span class='p'>${Math.round(p * 100)}%</span></div>`).join("")}</div>
    </details>` : "";
  els.drawer.innerHTML = `<div${swapStyle()}>${head}${relationship}${facts}${dossier}${contactSection}${confidence}</div>`;
}

// ---------- events ----------

els.viewport.addEventListener("scroll", scheduleRows, { passive: true });
window.addEventListener("resize", () => { scheduleRows(); moveInk(); });
// The viewport takes its final height after the first paint; re-render when it does.
new ResizeObserver(scheduleRows).observe(els.viewport);

root.addEventListener("click", async (event) => {
  const target = event.target;
  const tab = target.closest("[data-tab]");
  if (tab) { setTab(tab.dataset.tab); return; }
  const facetValue = target.closest("[data-facet-value]");
  if (facetValue) { toggleFilter(facetValue.dataset.facetKey, facetValue.dataset.facetValue); return; }
  const facetToggle = target.closest("[data-facet-toggle]");
  if (facetToggle) {
    const key = facetToggle.dataset.facetToggle;
    const open = state.collapsed.has(key);
    if (open) state.collapsed.delete(key); else state.collapsed.add(key);
    // Toggle in place so the body can animate shut or open.
    facetToggle.closest(".facet").dataset.open = String(open);
    facetToggle.setAttribute("aria-expanded", String(open));
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
  const sort = target.closest("[data-sort]");
  if (sort) {
    const key = sort.dataset.sort;
    state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : 1 };
    enterRows = true;
    renderAll();
    return;
  }
  if (target.matches("[data-select-all]")) { selectAllMatching(); return; }
  // The checkbox label swallows its own clicks; the input's click is the one that counts.
  const check = target.closest(".check");
  if (check) {
    if (target.matches("[data-select]")) toggleSelected(check.closest(".row").dataset.id);
    return;
  }
  const action = target.closest("[data-action]");
  if (action) {
    const name = action.dataset.action;
    if (name === "clear") clearSelection();
    else if (name === "undo") await undoLast();
    else await applyAction(name);
    return;
  }
  const one = target.closest("[data-one]");
  if (one && state.drawerId) { await applyAction(one.dataset.one, [state.drawerId]); return; }
  if (target.closest("[data-drawer-close]")) { closeDrawer(); return; }
  if (target.closest("[data-drawer-retry]") && state.drawerId) { void openDrawer(state.drawerId, { refresh: true }); return; }
  const row = target.closest(".row");
  if (row) {
    state.focus = Number(row.dataset.index);
    toggleDrawer(row.dataset.id);
  }
});

root.addEventListener("toggle", (event) => {
  if (event.target.matches("[data-hint]")) state.hintOpen = event.target.open;
}, true);

root.addEventListener("input", (event) => {
  if (event.target === els.search) {
    state.text = els.search.value;
    resetView();
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
  // A focused button, link or summary keeps its native Enter and Space.
  if ((event.key === "Enter" || event.key === " ") && event.target.matches("button, a, summary, label")) return;
  if (event.repeat && ["s", "p", "w", "z", "x", " ", "Enter"].includes(event.key)) return;
  switch (event.key) {
    case "/": event.preventDefault(); els.search.focus(); els.search.select(); break;
    case "f": event.preventDefault(); els.rail.querySelector(".facet-value")?.focus(); break;
    case "1": case "2": case "3": setTab(DECISIONS[Number(event.key) - 1]); break;
    case "j": case "ArrowDown": event.preventDefault(); moveFocus(1); break;
    case "k": case "ArrowUp": event.preventDefault(); moveFocus(-1); break;
    case "x": case " ": if (state.focus >= 0) { event.preventDefault(); toggleSelected(state.matching[state.focus].person_id); } break;
    case "A": if (event.shiftKey) { event.preventDefault(); selectAllMatching(); } break;
    case "s": if (state.selected.size) void applyAction("share"); break;
    case "p": if (state.selected.size) void applyAction("private"); break;
    case "w": if (state.selected.size) void applyAction("worth"); break;
    case "z": void undoLast(); break;
    case "Enter": if (state.focus >= 0) toggleDrawer(state.matching[state.focus].person_id); break;
    case "Escape":
      if (state.drawerId) closeDrawer();
      else if (state.selected.size) clearSelection();
      break;
    default: break;
  }
});

// ---------- boot ----------

(async () => {
  els.rows.innerHTML = "<div class='grid-loading' aria-busy='true'><span class='sr-only'>Loading people…</span>" + "<div class='skeleton'></div>".repeat(8) + "</div>";
  try {
    await load();
  } catch (error) {
    els.empty.hidden = false;
    els.empty.textContent = error.message;
    els.rows.innerHTML = "";
    return;
  }
  els.rows.innerHTML = "";
  restoreFilters();
  els.search.value = state.text;
  enterRows = true;
  renderAll();
})();
