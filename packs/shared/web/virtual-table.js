import { Virtualizer, observeElementRect, observeElementOffset, elementScroll } from "./vendor/tanstack-virtual-core.js";

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

// Both People and Search keep only the visible rows mounted. Search measures
// its wrapped reasoning; People supplies a fixed row height.
export class VirtualTable {
  constructor({ viewport, content, estimateSize, getKey, createRow, syncRow = () => {}, measure = false }) {
    this.viewport = viewport;
    this.content = content;
    this.items = [];
    this.nodes = new Map();
    this.getKey = getKey;
    this.createRow = createRow;
    this.syncRow = syncRow;
    this.measure = measure;
    this.enter = false;
    const spacer = () => {
      const node = document.createElement(content.tagName === "TBODY" ? "tr" : "div");
      node.className = "virtual-spacer";
      node.setAttribute("aria-hidden", "true");
      if (node.tagName === "TR") node.innerHTML = "<td colspan='2'></td>";
      return node;
    };
    this.before = spacer();
    this.after = spacer();
    content.replaceChildren(this.before, this.after);
    this.virtualizer = new Virtualizer({
      count: 0, getScrollElement: () => viewport, estimateSize,
      getItemKey: (index) => getKey(this.items[index]),
      observeElementRect, observeElementOffset, scrollToFn: elementScroll,
      overscan: 5, onChange: () => this.schedule(),
    });
    this.virtualizer._didMount();
    this.virtualizer._willUpdate();
  }

  setItems(items, { animate = false, reset = false } = {}) {
    this.items = items;
    this.enter = animate;
    this.virtualizer.setOptions({ ...this.virtualizer.options, count: items.length,
      getItemKey: (index) => this.getKey(items[index]) });
    this.viewport.setAttribute("aria-rowcount", String(items.length));
    if (reset) this.virtualizer.scrollToOffset(0);
    this.schedule();
  }

  schedule() {
    if (this.queued) return;
    this.queued = true;
    const run = () => { this.queued = false; this.render(); };
    // A hidden tab gets no animation frames; render on a timer so the page is ready when it shows.
    if (document.visibilityState === "hidden") setTimeout(run, 0);
    else requestAnimationFrame(run);
  }

  render() {
    const virtual = this.virtualizer.getVirtualItems();
    const keep = new Set(virtual.map((item) => item.key));
    for (const [key, node] of this.nodes) {
      if (keep.has(key)) continue;
      node.remove();
      this.nodes.delete(key);
    }
    this.before.style.height = `${virtual[0]?.start || 0}px`;
    this.after.style.height = `${Math.max(0, this.virtualizer.getTotalSize() - (virtual.at(-1)?.end || 0))}px`;
    let cursor = this.before.nextSibling;
    virtual.forEach((item, position) => {
      let node = this.nodes.get(item.key);
      if (!node) {
        node = this.createRow(this.items[item.index]);
        this.nodes.set(item.key, node);
      }
      node.dataset.index = item.index;
      node.setAttribute("aria-rowindex", String(item.index + 1));
      this.syncRow(node, this.items[item.index], item.index);
      if (node === cursor) cursor = cursor.nextSibling;
      else this.content.insertBefore(node, cursor);
      if (this.enter && position < 14 && !reducedMotion.matches) {
        node.getAnimations().forEach((animation) => animation.cancel());
        node.animate([{ opacity: 0, translate: "0 6px" }, { opacity: 1, translate: "0 0" }],
          { duration: 200, delay: Math.min(position * 14, 100), easing: "cubic-bezier(.2,0,0,1)", fill: "backwards" });
      }
      if (this.measure) this.virtualizer.measureElement(node);
    });
    this.enter = false;
  }
}
