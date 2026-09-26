import { useCallback, useState } from "react";

import { usePersonDetail } from "./usePersonDetail";

/**
 * Which person the drawer shows. `openId` is null while closed; `shownId` keeps the
 * last person so the panel can animate shut with them still in it.
 */
export function useDrawer() {
  const [openId, setOpenId] = useState<string | null>(null);
  const [shownId, setShownId] = useState<string | null>(null);
  const { state: detail, refresh } = usePersonDetail(openId);

  const open = useCallback((id: string) => {
    setOpenId(id);
    setShownId(id);
  }, []);
  const close = useCallback(() => setOpenId(null), []);
  // The same person closes; anyone else opens (or switches to) them.
  const toggle = useCallback((id: string) => {
    setOpenId((current) => (current === id ? null : id));
    setShownId(id);
  }, []);

  return { openId, shownId, detail, open, close, toggle, refresh };
}
