import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"

import { Button } from "@/components/ui/button"
import { fetchFirstLook } from "@/lib/api/install"
import { HOME } from "@/lib/nav"

import { Checklist } from "./Checklist"
import { FirstLookPanel } from "./FirstLookPanel"
import { Frame, Info, Note } from "./Frame"
import { leftText } from "./pace"
import { importsMessages, useRun } from "./run"

const PEOPLE_POLL_MS = 5_000

/** Import: the run over every source Sources' Start began (and the side nav's Import card opens),
 *  in the background, as a checklist; the first look once people are in. A LinkedIn run's record
 *  is Set up's, never shown here: until this run writes its own, the screen says Starting. */
export function Import() {
  const navigate = useNavigate()
  const run = useRun(importsMessages)
  const { data, ready, left } = run
  const processing = data !== undefined && !ready
  const look = useQuery({
    queryKey: ["onboarding", "first-look"],
    queryFn: ({ signal }) => fetchFirstLook(signal),
    enabled: processing || ready,
    refetchInterval: processing ? PEOPLE_POLL_MS : false,
    retry: false,
  })
  const known = look.data?.total ?? 0
  return (
    <Frame
      step="import"
      left={
        <>
          <Checklist run={run} />
          {processing && data.status !== "failed" ? (
            <div className="mt-2 flex flex-col gap-2">
              <Button variant="primary" className="self-start" onClick={() => void navigate(HOME.href)}>
                Start using Powerpacks now
              </Button>
              <Note>
                The import keeps running in the background; Import in the side nav shows where it is.
                {left !== null ? ` This stage: ${leftText(left)}.` : ""}
              </Note>
            </div>
          ) : null}
        </>
      }
      right={
        known > 0 || ready ? (
          <FirstLookPanel
            look={look.data ?? { people: [], total: 0, questions: [] }}
            ready={ready}
            sourcesNext={false}
          />
        ) : (
          <Info
            title="While you wait"
            lines={[
              "Each source adds the people you talk to; nothing already done is redone",
              "Closing the window does not stop anything",
              "You can use Powerpacks meanwhile; Import in the side nav shows where it is",
            ]}
          />
        )
      }
    />
  )
}
