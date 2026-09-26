import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

// While the detail loads: a titled block with two shimmer lines.
export function DetailLoading() {
  return (
    <div className="dsec" aria-busy="true">
      <h3>Details</h3>
      <p className="dim">Loading details…</p>
      <Skeleton className="h-3 w-[70%]" />
      <Skeleton className="h-3 w-1/2" />
    </div>
  );
}

export function DetailFailed({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="dsec">
      <h3>Details</h3>
      <p className="dim">
        Couldn't load details.{" "}
        <Button variant="ghost" className="ml-1 min-h-[26px] px-2" data-drawer-retry onClick={onRetry}>Retry</Button>
      </p>
    </div>
  );
}
