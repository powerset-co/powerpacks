import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react"
import { Outlet, useLocation, useMatch, useNavigate, useSearchParams } from "react-router-dom"

import { EmptyState, Toast, type ToastMessage } from "@/components/shared"
import { useInert } from "@/hooks/useInert"
import {
  LOGBOOK_PATH,
  openedFromPeople,
  readerHref,
  readerScope,
  type ReaderState,
} from "@/lib/people/logbook"

import { useLogbook } from "./hooks/useLogbook"
import { usePeopleQuery } from "./hooks/usePeopleQuery"
import type { ReaderContext } from "./logbook/LogbookReader"
import { PeopleLoading } from "./PeopleLoading"
import { PeopleShell } from "./PeopleShell"
import { PeopleWorkspace } from "./PeopleWorkspace"
import "./styles/page.css"

/**
 * /people and /people/logbook: the Logbook reader (the outlet) covers the People list without
 * unmounting it, so going back finds the same tab, filters, sort, selection, open drawer and
 * scroll. Covered, the list is hidden and inert, keeps its geometry, and takes no keys.
 * The page owns the one Logbook build, so the list and the reader start and follow the same
 * build; its toasts sit outside the list and read over either.
 */
export function PeoplePage() {
  const reading = useMatch(LOGBOOK_PATH) !== null
  const people = usePeopleQuery()
  const list = useRef<HTMLDivElement>(null)
  const returnTo = useRef<Element | null>(null)

  // The control that opened the reader gets focus back when the list shows again: noted
  // before the reader's own effect moves focus, restored after the list stops being inert.
  useLayoutEffect(() => {
    if (reading) returnTo.current = document.activeElement
  }, [reading])
  useInert(list, reading)
  useEffect(() => {
    if (reading) return
    const back = returnTo.current
    returnTo.current = null
    if (back instanceof HTMLElement && back.isConnected) back.focus({ preventScroll: true })
  }, [reading])

  // From People the reader opens as a new history entry, so Back returns to the list. A build
  // finishing while the reader is open adds what it saved to the scope in place (an unscoped
  // reader already shows everything).
  const navigate = useNavigate()
  const location = useLocation()
  const [params] = useSearchParams()
  const openLogbooks = useCallback(
    (slugs: readonly string[]) => {
      const state: ReaderState = { fromPeople: true }
      void navigate(readerHref(slugs), { state })
    },
    [navigate],
  )
  const openBuilt = useCallback(
    (slugs: readonly string[]) => {
      if (!reading) {
        openLogbooks(slugs)
        return
      }
      const scope = readerScope(params)
      if (!scope.length) return
      const state: ReaderState = { fromPeople: openedFromPeople(location.state) }
      void navigate(readerHref([...new Set([...scope, ...slugs])]), { state, replace: true })
    },
    [reading, params, location.state, navigate, openLogbooks],
  )
  const [toast, setToast] = useState<ToastMessage | null>(null)
  const dismiss = useCallback(() => setToast(null), [])
  const logbook = useLogbook(setToast, openBuilt)
  const context: ReaderContext = { building: logbook.building, build: logbook.build }

  let content
  if (people.data) {
    content = (
      <PeopleWorkspace people={people.data} reading={reading} logbook={logbook} onView={openLogbooks} />
    )
  } else if (people.error) {
    content = <PeopleShell rail={null} main={<EmptyState data-empty>{people.error.message}</EmptyState>} />
  } else content = <PeopleLoading />

  return (
    <div className="people-page" data-reading={reading || undefined}>
      <div ref={list} className="people-page-list" aria-hidden={reading || undefined}>
        {content}
      </div>
      <Outlet context={context} />
      <Toast toast={toast} onDismiss={dismiss} className="toast leading-[1.45]" />
    </div>
  )
}
