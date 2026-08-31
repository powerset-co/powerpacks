const toast = document.querySelector(".toast");

function announce(message, isError = false) {
  if (!toast) return;
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("show");
  window.clearTimeout(announce.timer);
  announce.timer = window.setTimeout(
    () => toast.classList.remove("show"), isError ? 6000 : 1800);
}

async function post(path, values) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(values),
  });
  if (!response.ok) {
    const text = (await response.text()) || "Could not send feedback";
    let message = text;
    try {
      const payload = JSON.parse(text);
      message = payload.error || payload.status || text;
    } catch { /* plain text */ }
    throw new Error(message);
  }
  return response.json();
}

function closeFeedbackPopover() {
  document.querySelector(".feedback-popover")?.remove();
}

const LAZY_BATCH = 100;

const revealObserver = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (!entry.isIntersecting) return;
    const sentinel = entry.target;
    const table = sentinel.closest("table");
    const hiddenRows = table.querySelectorAll("tr[data-lazy][hidden]");
    for (let i = 0; i < LAZY_BATCH && i < hiddenRows.length; i += 1) {
      hiddenRows[i].hidden = false;
      hiddenRows[i].removeAttribute("data-lazy");
    }
    if (hiddenRows.length <= LAZY_BATCH) {
      revealObserver.unobserve(sentinel);
      sentinel.remove();
    }
  });
});

function watchLazyRows(root) {
  root.querySelectorAll(".lazy-sentinel").forEach((sentinel) => revealObserver.observe(sentinel));
}

const TAGGED_PREFIX = "powerset_tagged_";
const LEGACY_PINNED_PREFIX = "powerset_pinned_";
const TAG_NAME_MAX = 40;
const LEGACY_PIN_TAG = "Pinned";
const CSV_HEADERS = ["Name", "Title", "Company", "Location", "Sources", "Network", "Email Count", "Reasoning"];
const FILLER_WORDS = new Set([
  "a", "an", "the", "in", "at", "on", "for", "to", "of", "and", "or", "with", "who",
  "are", "is", "that", "from", "by", "as", "my", "our", "find", "search", "looking",
  "look", "get", "me", "i", "want",
]);

function taggedKey(body) {
  return `${TAGGED_PREFIX}${body.dataset.searchBody}`;
}

function readTagged(body) {
  try {
    const value = JSON.parse(localStorage.getItem(taggedKey(body)) || "null");
    if (value && Array.isArray(value.tags) && value.assignments
        && typeof value.assignments === "object") return value;
  } catch { /* fall through to legacy migration */ }
  try {
    const key = `${LEGACY_PINNED_PREFIX}${body.dataset.searchBody}`;
    const ids = JSON.parse(localStorage.getItem(key) || "null");
    if (Array.isArray(ids) && ids.length) {
      const assignments = {};
      ids.forEach((id) => { if (typeof id === "string") assignments[id] = [LEGACY_PIN_TAG]; });
      const migrated = { tags: [LEGACY_PIN_TAG], assignments };
      localStorage.setItem(taggedKey(body), JSON.stringify(migrated));
      localStorage.removeItem(key);
      return migrated;
    }
  } catch { /* ignore */ }
  return { tags: [], assignments: {} };
}

function writeTagged(body, data) {
  if (!data.tags.length && !Object.keys(data.assignments).length) {
    localStorage.removeItem(taggedKey(body));
  } else {
    localStorage.setItem(taggedKey(body), JSON.stringify(data));
  }
}

function normalizeTag(value) {
  return value.trim().slice(0, TAG_NAME_MAX);
}

function existingTag(tags, candidate) {
  const lower = candidate.toLowerCase();
  return tags.find((tag) => tag.toLowerCase() === lower);
}

function toggleTag(body, personId, rawTag) {
  const tag = normalizeTag(rawTag);
  if (!tag) return;
  const data = readTagged(body);
  const canonical = existingTag(data.tags, tag) || tag;
  if (!data.tags.includes(canonical)) data.tags.push(canonical);
  const current = data.assignments[personId] || [];
  const next = current.includes(canonical)
    ? current.filter((value) => value !== canonical)
    : [...current, canonical];
  if (next.length) data.assignments[personId] = next;
  else delete data.assignments[personId];
  writeTagged(body, data);
}

function removeTag(body, rawTag) {
  const data = readTagged(body);
  const canonical = existingTag(data.tags, normalizeTag(rawTag));
  if (!canonical) return;
  data.tags = data.tags.filter((tag) => tag !== canonical);
  Object.entries(data.assignments).forEach(([personId, tags]) => {
    const next = tags.filter((tag) => tag !== canonical);
    if (next.length) data.assignments[personId] = next;
    else delete data.assignments[personId];
  });
  writeTagged(body, data);
}

function taggedRows(body, toolbar) {
  const data = readTagged(body);
  const filters = toolbar?._selectedTagFilters || new Set();
  const rows = new Map();
  toolbar.closest("[data-pond-panel]").querySelectorAll(".candidate-row[data-person-id]").forEach((row) => {
    const tags = data.assignments[row.dataset.personId] || [];
    if (!tags.length || (filters.size && !tags.some((tag) => filters.has(tag)))) return;
    const prior = rows.get(row.dataset.personId);
    if (!prior || Number(row.dataset.personScore) > Number(prior.dataset.personScore)) {
      rows.set(row.dataset.personId, row);
    }
  });
  return [...rows.values()];
}

function renderTagFilters(toolbar, tags) {
  const host = toolbar.querySelector("[data-tag-filters]");
  const selected = toolbar._selectedTagFilters ||= new Set();
  [...selected].forEach((tag) => { if (!tags.includes(tag)) selected.delete(tag); });
  host.replaceChildren();
  const label = document.createElement("span");
  label.textContent = "Filter:";
  host.append(label);
  tags.forEach((tag) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `tag-filter${selected.has(tag) ? " selected" : ""}`;
    button.dataset.filterTag = tag;
    button.setAttribute("aria-pressed", String(selected.has(tag)));
    button.textContent = tag;
    host.append(button);
  });
  if (selected.size) {
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "clear-tag-filter";
    clear.dataset.clearTagFilter = "";
    clear.textContent = "Clear filter";
    host.append(clear);
  }
}

function updateTags(body) {
  const data = readTagged(body);
  body.querySelectorAll("[data-tag-person]").forEach((button) => {
    const tags = data.assignments[button.dataset.tagPerson] || [];
    const host = button.querySelector("[data-person-tags]");
    host.replaceChildren(...tags.map((tag) => {
      const chip = document.createElement("span");
      chip.className = "person-tag";
      chip.textContent = tag;
      chip.title = tag;
      return chip;
    }));
    button.classList.toggle("tagged", tags.length > 0);
    const name = button.closest("tr").dataset.personName;
    button.setAttribute("aria-label", `${tags.length ? "Edit tags for" : "Add tag to"} ${name}`);
    button.title = tags.length ? "Edit tags" : "Add tag";
  });
  body.querySelectorAll("[data-results-toolbar]").forEach((toolbar) => {
    const panel = toolbar.closest("[data-pond-panel]");
    const available = new Set([...panel.querySelectorAll("[data-person-id]")]
      .map((row) => row.dataset.personId));
    const count = Object.entries(data.assignments)
      .filter(([id, tags]) => available.has(id) && tags.length).length;
    if (!count) toolbar.dataset.tagFilter = "all";
    const taggedOnly = toolbar.dataset.tagFilter === "tagged";
    const filters = toolbar._selectedTagFilters ||= new Set();
    panel.querySelectorAll(".candidate-row[data-person-id]").forEach((row) => {
      const tags = data.assignments[row.dataset.personId] || [];
      const matches = tags.length && (!filters.size || tags.some((tag) => filters.has(tag)));
      if (taggedOnly && matches) row.removeAttribute("data-lazy");
      row.hidden = taggedOnly ? !matches : row.hasAttribute("data-lazy");
    });
    panel.querySelectorAll(".lazy-sentinel").forEach((row) => { row.hidden = taggedOnly; });
    toolbar.querySelectorAll("[data-tagged-count]").forEach((node) => { node.textContent = count; });
    toolbar.querySelector("[data-result-filter='tagged']").hidden = !count;
    renderTagFilters(toolbar, data.tags);
    toolbar.querySelector("[data-tag-filters]").hidden = !taggedOnly || !data.tags.length;
    const hasRows = taggedOnly && taggedRows(body, toolbar).length > 0;
    toolbar.querySelector("[data-untag-all]").hidden = !hasRows;
    toolbar.querySelector("[data-copy-results]").hidden = !hasRows;
    toolbar.querySelector("[data-export-csv]").hidden = !hasRows;
    toolbar.querySelector("[data-clear-tags]").hidden = !hasRows || toolbar.dataset.confirmClear === "true";
    toolbar.querySelector("[data-clear-tags-confirm]").hidden = toolbar.dataset.confirmClear !== "true";
    toolbar.querySelectorAll("[data-result-filter]").forEach((button) => {
      const selected = button.dataset.resultFilter === toolbar.dataset.tagFilter;
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  });
}

function escapeCsv(value) {
  const text = String(value ?? "");
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function csvFilename(title) {
  const words = title.toLowerCase().replace(/[^a-z0-9\s-]/g, "").split(/\s+/)
    .filter((word) => word && !FILLER_WORDS.has(word)).slice(0, 5);
  return `${words.join("-") || "results"}_${new Date().toISOString().slice(0, 10)}.csv`;
}

function exportValues(rows) {
  return rows.map((row) => {
    const data = row.dataset;
    const name = data.personLinkedin
      ? `=HYPERLINK("${data.personLinkedin.replaceAll('"', '""')}","${data.personName.replaceAll('"', '""')}")`
      : data.personName;
    return [name, data.personTitle, data.personCompany, data.personLocation, data.personSource,
      data.personNetwork, "", data.personReasoning];
  });
}

function exportTagged(body, toolbar) {
  const values = exportValues(taggedRows(body, toolbar));
  const csv = [CSV_HEADERS, ...values].map((row) => row.map(escapeCsv).join(",")).join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8;" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = csvFilename(body.dataset.searchTitle || "results");
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  announce(`Exported ${values.length} tagged result${values.length === 1 ? "" : "s"}`);
}

function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

async function copyTagged(body, toolbar) {
  const rows = taggedRows(body, toolbar);
  const values = exportValues(rows);
  const htmlRows = rows.map((row, index) => {
    const data = row.dataset;
    const cells = values[index].map((value, cellIndex) => {
      if (cellIndex === 0 && data.personLinkedin) {
        return `<td><a href="${escapeHtml(data.personLinkedin)}">${escapeHtml(data.personName)}</a></td>`;
      }
      return `<td>${escapeHtml(value)}</td>`;
    });
    return `<tr>${cells.join("")}</tr>`;
  });
  const html = `<table><thead><tr>${CSV_HEADERS.map((header) => `<th>${header}</th>`).join("")}</tr></thead><tbody>${htmlRows.join("")}</tbody></table>`;
  const plain = [CSV_HEADERS, ...values.map((row, index) => [rows[index].dataset.personName, ...row.slice(1)])]
    .map((row) => row.join("\t")).join("\n");
  await navigator.clipboard.write([new ClipboardItem({
    "text/html": new Blob([html], { type: "text/html" }),
    "text/plain": new Blob([plain], { type: "text/plain" }),
  })]);
  announce(`Copied ${values.length} tagged result${values.length === 1 ? "" : "s"}`);
}

function closeTagPopover() {
  document.querySelector(".tag-popover")?.remove();
}

function tagPopover(anchor, body) {
  closeTagPopover();
  const pop = document.createElement("div");
  pop.className = "tag-popover";
  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = TAG_NAME_MAX;
  input.placeholder = "Add tag...";
  input.setAttribute("aria-label", "Add tag");
  const list = document.createElement("div");
  list.className = "tag-options";
  pop.append(input, list);

  function apply(tag) {
    toggleTag(body, anchor.dataset.tagPerson, tag);
    input.value = "";
    updateTags(body);
    render();
  }

  function render() {
    const data = readTagged(body);
    const applied = new Set(data.assignments[anchor.dataset.tagPerson] || []);
    const trimmed = normalizeTag(input.value);
    const filtered = trimmed
      ? data.tags.filter((tag) => tag.toLowerCase().includes(trimmed.toLowerCase()))
      : data.tags;
    const canCreate = trimmed && !existingTag(data.tags, trimmed);
    list.replaceChildren();
    if (!filtered.length && !canCreate) {
      const empty = document.createElement("p");
      empty.className = "tag-options-empty";
      empty.textContent = data.tags.length ? "No matching tags" : "No tags yet — type to create one.";
      list.append(empty);
    }
    filtered.forEach((tag) => {
      const row = document.createElement("div");
      row.className = `tag-option${applied.has(tag) ? " applied" : ""}`;
      const label = document.createElement("span");
      label.className = "tag-option-label";
      label.textContent = `${applied.has(tag) ? "✓" : ""} ${tag}`;
      label.addEventListener("click", () => apply(tag));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove-tag";
      remove.setAttribute("aria-label", `Remove ${tag} from search`);
      remove.title = "Remove tag from search";
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        removeTag(body, tag);
        updateTags(body);
        render();
      });
      row.append(label, remove);
      list.append(row);
    });
    if (canCreate) {
      const create = document.createElement("button");
      create.type = "button";
      create.className = "create-tag";
      create.textContent = `Create “${trimmed}”`;
      create.addEventListener("click", () => apply(trimmed));
      list.append(create);
    }
  }

  input.addEventListener("input", render);
  input.addEventListener("keydown", (event) => {
    event.stopPropagation();
    if (event.key === "Escape") closeTagPopover();
    if (event.key !== "Enter") return;
    event.preventDefault();
    const data = readTagged(body);
    const trimmed = normalizeTag(input.value);
    const filtered = data.tags.filter((tag) => tag.toLowerCase().includes(trimmed.toLowerCase()));
    if (trimmed && !existingTag(data.tags, trimmed)) apply(trimmed);
    else if (filtered.length === 1) apply(filtered[0]);
  });
  pop.addEventListener("click", (event) => event.stopPropagation());
  anchor.closest(".candidate-person-cell").append(pop);
  render();
  window.setTimeout(() => input.focus(), 0);

  function away(event) {
    if (!document.body.contains(pop)) {
      document.removeEventListener("click", away);
      return;
    }
    if (!pop.contains(event.target) && event.target !== anchor) {
      document.removeEventListener("click", away);
      pop.remove();
    }
  }
  window.setTimeout(() => document.addEventListener("click", away), 0);
}

async function loadSearchDetails(body) {
  if (!body || body.dataset.loaded === "true" || body.dataset.loading === "true") return;
  body.dataset.loading = "true";
  try {
    const response = await fetch(`/api/search?run_id=${encodeURIComponent(body.dataset.searchBody)}`);
    if (!response.ok) throw new Error((await response.text()) || "Could not load results");
    body.innerHTML = await response.text();
    body.dataset.loaded = "true";
    watchLazyRows(body);
    updateTags(body);
  } catch (error) {
    body.innerHTML = `<p class='loading-results'>${error.message}</p>`;
    announce(error.message, true);
  } finally {
    delete body.dataset.loading;
  }
}

function feedbackPopover(anchor) {
  closeFeedbackPopover();
  const host = anchor.closest(".candidate-indicators, .search-card") || document.body;
  const pop = document.createElement("div");
  pop.className = "feedback-popover";

  const label = document.createElement("p");
  label.className = "feedback-context";
  label.textContent = anchor.getAttribute("aria-label") || "Send feedback";
  const confirm = document.createElement("p");
  confirm.className = "feedback-confirm";
  confirm.textContent = "Send this feedback to Powerset?";
  const textarea = document.createElement("textarea");
  textarea.rows = 2;
  textarea.maxLength = 4000;
  textarea.setAttribute("aria-label", "Feedback note");
  textarea.placeholder = "Identifiers only — never include message content.";
  const footer = document.createElement("div");
  footer.className = "feedback-footer";
  footer.innerHTML = "<span class='feedback-hint'>&#8629; &#8984;+Enter</span>"
    + "<span class='feedback-actions'>"
    + "<button type='button' class='feedback-cancel'>Cancel</button>"
    + "<button type='button' class='feedback-send' disabled>Send</button>"
    + "</span>";
  pop.append(label, confirm, textarea, footer);

  const send = footer.querySelector(".feedback-send");
  const cancel = footer.querySelector(".feedback-cancel");
  let settled = false;

  function finish() {
    if (settled) return;
    settled = true;
    document.removeEventListener("click", away);
    pop.remove();
  }

  async function submit() {
    const comment = textarea.value.trim();
    if (!comment || settled) return;
    send.disabled = true;
    cancel.disabled = true;
    try {
      await post("/feedback", {
        run_id: anchor.dataset.feedbackRun || "",
        person_id: anchor.dataset.feedbackPerson || "",
        comment,
      });
    } catch (error) {
      announce(error.message, true);
      send.disabled = false;
      cancel.disabled = false;
      return;
    }
    settled = true;
    document.removeEventListener("click", away);
    pop.className = "feedback-popover feedback-done";
    pop.innerHTML = "<span class='feedback-done-badge'><svg viewBox='0 0 24 24' width='14' height='14' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><path d='M20 6 9 17l-5-5'/></svg></span><p>Got it, thanks!</p>";
    window.setTimeout(() => pop.remove(), 900);
  }

  textarea.addEventListener("input", () => {
    send.disabled = !textarea.value.trim();
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 140) + "px";
  });
  textarea.addEventListener("keydown", (event) => {
    event.stopPropagation();
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void submit();
    }
    if (event.key === "Escape") finish();
  });
  send.addEventListener("click", () => void submit());
  cancel.addEventListener("click", finish);
  pop.addEventListener("click", (event) => event.stopPropagation());

  host.append(pop);
  const hostRect = host.getBoundingClientRect();
  const anchorRect = anchor.getBoundingClientRect();
  pop.style.top = `${anchorRect.bottom - hostRect.top + host.scrollTop + 8}px`;
  pop.style.right = `${Math.max(8, hostRect.right - anchorRect.right)}px`;
  window.setTimeout(() => textarea.focus(), 80);

  function away(event) {
    if (!document.body.contains(pop)) {
      document.removeEventListener("click", away);
      return;
    }
    if (!pop.contains(event.target) && event.target !== anchor) finish();
  }
  window.setTimeout(() => document.addEventListener("click", away), 0);
}

document.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-pond-tab]");
  if (tab) {
    const body = tab.closest(".search-body");
    body.querySelectorAll("[data-pond-tab]").forEach((button) => {
      button.setAttribute("aria-selected", String(button === tab));
    });
    body.querySelectorAll("[data-pond-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.pondPanel !== tab.dataset.pondTab;
    });
    closeTagPopover();
    return;
  }
  const trigger = event.target.closest("[data-feedback-run]");
  if (!trigger) return;
  event.preventDefault();
  event.stopPropagation();
  feedbackPopover(trigger);
});

document.addEventListener("click", (event) => {
  const body = event.target.closest(".search-body");
  if (!body) return;
  const tag = event.target.closest("[data-tag-person]");
  if (tag) {
    event.stopPropagation();
    tagPopover(tag, body);
    return;
  }
  const filter = event.target.closest("[data-result-filter]");
  if (filter) {
    filter.closest("[data-results-toolbar]").dataset.tagFilter = filter.dataset.resultFilter;
    updateTags(body);
    return;
  }
  const toolbar = event.target.closest("[data-results-toolbar]");
  const tagFilter = event.target.closest("[data-filter-tag]");
  if (tagFilter) {
    const filters = toolbar._selectedTagFilters ||= new Set();
    if (filters.has(tagFilter.dataset.filterTag)) filters.delete(tagFilter.dataset.filterTag);
    else filters.add(tagFilter.dataset.filterTag);
    updateTags(body);
    return;
  }
  if (event.target.closest("[data-clear-tag-filter]")) {
    toolbar._selectedTagFilters.clear();
    updateTags(body);
    return;
  }
  if (event.target.closest("[data-untag-all]")) {
    const data = readTagged(body);
    toolbar.closest("[data-pond-panel]").querySelectorAll(".candidate-row:not([hidden])").forEach((row) => {
      delete data.assignments[row.dataset.personId];
    });
    writeTagged(body, data);
    updateTags(body);
    return;
  }
  if (event.target.closest("[data-copy-results]")) {
    void copyTagged(body, toolbar).catch(() => announce("Failed to copy", true));
    return;
  }
  if (event.target.closest("[data-export-csv]")) {
    exportTagged(body, toolbar);
    return;
  }
  if (event.target.closest("[data-clear-tags]")) {
    toolbar.dataset.confirmClear = "true";
    updateTags(body);
    return;
  }
  if (event.target.closest("[data-confirm-clear-tags]")) {
    writeTagged(body, { tags: [], assignments: {} });
    delete toolbar.dataset.confirmClear;
    updateTags(body);
    return;
  }
  if (event.target.closest("[data-cancel-clear-tags]")) {
    delete toolbar.dataset.confirmClear;
    updateTags(body);
  }
});

document.querySelectorAll("[data-search-body]").forEach((body) => void loadSearchDetails(body));

document.addEventListener("error", (event) => {
  if (event.target.matches?.(".avatar img")) event.target.hidden = true;
}, true);

function closeDetails() {
  document.querySelectorAll(".person-details:not([hidden])").forEach((panel) => {
    panel.hidden = true;
  });
}

document.addEventListener("click", (event) => {
  const trigger = event.target.closest(".details-trigger");
  if (trigger) {
    event.preventDefault();
    event.stopPropagation();
    const panel = trigger.closest(".candidate-indicators")?.querySelector(".person-details");
    if (!panel) return;
    const willOpen = panel.hidden;
    closeDetails();
    closeFeedbackPopover();
    panel.hidden = !willOpen;
    return;
  }
  const more = event.target.closest(".show-more");
  if (more) {
    event.stopPropagation();
    const text = more.previousElementSibling;
    const clamped = text.classList.toggle("about-clamped");
    more.textContent = clamped ? "Show more" : "Show less";
    return;
  }
  if (!event.target.closest(".person-details, .feedback-popover, .tag-popover")) closeDetails();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeDetails();
    closeTagPopover();
  }
});

document.addEventListener("click", (event) => {
  const copy = event.target.closest("[data-copy-query]");
  if (!copy) return;
  event.stopPropagation();
  void navigator.clipboard.writeText(copy.dataset.copyQuery).then(() => {
    const prior = copy.textContent;
    copy.textContent = "✓";
    setTimeout(() => { copy.textContent = prior; }, 900);
  });
}, true);
