import type { ReactNode } from "react";

// One term and its value in a .kv list; nothing at all when there is no value.
export function Pair({ term, children }: { term: string; children: ReactNode }) {
  if (children === null || children === undefined || children === false || children === "") return null;
  return (
    <>
      <dt>{term}</dt>
      <dd>{children}</dd>
    </>
  );
}

// Values one per line, as the legacy page joined them with <br>.
export function Lines({ values }: { values: readonly string[] }) {
  return <>{values.map((value, position) => <span key={position}>{position ? <br /> : null}{value}</span>)}</>;
}
