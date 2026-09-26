import { Kbd } from "@/components/shared";

const SHORTCUTS: readonly (readonly [readonly string[], string])[] = [
  [["1", "2", "3"], "Switch tab"],
  [["/"], "Search"],
  [["J", "K"], "Move"],
  [["X"], "Select"],
  [["⇧A"], "Select all matching"],
  [["S"], "Share"],
  [["P"], "Keep private"],
  [["W"], "Use worth"],
  [["Z"], "Undo"],
  [["Enter"], "Open or close details"],
];

export function ShortcutsHint() {
  return (
    <details className="rail-hint" data-hint>
      <summary>Keyboard shortcuts</summary>
      <dl>
        {SHORTCUTS.map(([keys, action]) => (
          <div key={action} className="contents">
            <dt>{keys.map((key) => <Kbd key={key}>{key}</Kbd>)}</dt>
            <dd>{action}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
