import { Button } from "@/components/ui/button";
import type { Decision } from "@/types/people";

const NOBODY: Record<Decision, string> = {
  confirm: "No one needs confirmation.",
  yes: "No people marked for sharing.",
  no: "No people marked as not sharing.",
};

interface EmptyTextProps {
  hasPeople: boolean;
  filtered: boolean;
  tab: Decision;
  onClear: () => void;
}

export function EmptyText({ hasPeople, filtered, tab, onClear }: EmptyTextProps) {
  if (!hasPeople) return <>No people to review yet. Run <code>bin/deep-context share</code>, then reload.</>;
  if (!filtered) return <>{NOBODY[tab]}</>;
  return (
    <>
      No people match these filters.{" "}
      <Button variant="ghost" data-clear-filters onClick={onClear}>Clear filters</Button>
    </>
  );
}
