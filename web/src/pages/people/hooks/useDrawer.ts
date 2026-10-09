import { useCallback, useState } from "react"

import { toggled } from "@/lib/sets"

import { OPEN_BY_DEFAULT, type SectionState } from "../drawer/sections"
import { usePersonDetail } from "./usePersonDetail"

/**
 * Which person the drawer shows. `openId` is null while closed; `shownId` keeps the
 * last person so the panel can animate shut with them still in it.
 */
export function useDrawer(params: URLSearchParams) {
  const [openId, setOpenId] = useState(() => params.get("person"))
  const [shownId, setShownId] = useState(() => params.get("person"))
  const [sections, setSections] = useState<ReadonlySet<string>>(
    () => new Set(params.get("sections")?.split(",").filter(Boolean) ?? OPEN_BY_DEFAULT),
  )
  const section: SectionState = useCallback(
    (key) => ({
      open: sections.has(key),
      onToggle: (next) => setSections((current) => toggled(current, key, next)),
    }),
    [sections],
  )
  const { state: detail, refresh } = usePersonDetail(openId)

  const close = useCallback(() => setOpenId(null), [])
  const open = useCallback((id: string) => {
    setOpenId(id)
    setShownId(id)
  }, [])
  // The same person closes; anyone else opens (or switches to) them.
  const toggle = useCallback((id: string) => {
    setOpenId((current) => (current === id ? null : id))
    setShownId(id)
  }, [])

  return { openId, shownId, detail, open, close, toggle, refresh, sections, section }
}
