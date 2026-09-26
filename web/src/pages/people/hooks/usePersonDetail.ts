import { useCallback, useEffect, useRef, useState } from "react";

import { fetchPersonDetail } from "@/lib/api/people";
import type { PersonDetail } from "@/types/people";

export type DetailState =
  | { status: "loading" }
  | { status: "failed" }
  | { status: "ready"; detail: PersonDetail };

const LOADING: DetailState = { status: "loading" };

/**
 * The drawer's detail for one person. A new person starts from loading; a refresh
 * (after a write, or Retry) keeps what is shown until the answer lands. The previous
 * request is aborted on every switch, refresh and close.
 */
export function usePersonDetail(id: string | null) {
  const [state, setState] = useState<DetailState>(LOADING);
  const [refreshes, setRefreshes] = useState(0);
  const shownId = useRef<string | null>(null);

  useEffect(() => {
    if (id === null) return;
    if (shownId.current !== id) setState(LOADING);
    shownId.current = id;
    const request = new AbortController();
    fetchPersonDetail(id, request.signal).then(
      (detail) => setState({ status: "ready", detail }),
      (error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setState({ status: "failed" });
      },
    );
    return () => request.abort();
  }, [id, refreshes]);

  const refresh = useCallback(() => setRefreshes((count) => count + 1), []);

  // Closed (null): the last person's state stays, so the drawer can animate out with it.
  return { state: id === null || id === shownId.current ? state : LOADING, refresh };
}
