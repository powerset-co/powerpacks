import { useQuery } from "@tanstack/react-query"

import { fetchFirstLook } from "@/lib/api/install"

import { Checklist } from "./Checklist"
import { FirstLookPanel } from "./FirstLookPanel"
import { Frame, Info } from "./Frame"
import { PageShot } from "./PageShot"
import { importsMessages, useRun } from "./run"

const PEOPLE_POLL_MS = 5_000

/** Set up: the LinkedIn run LinkedIn's Continue started, in the background, as a checklist; the
 *  page picture while the connections are read, then the first look once people are in. An
 *  import run's record (Sources' Start) is Import's, never shown here. */
export function SetUp() {
  const run = useRun((data) => !importsMessages(data))
  const { data, ready, count } = run
  // The page picture's count is the LinkedIn read's alone, not a later step's.
  const read = data?.step === "linkedin" ? count : null
  const processing = data !== undefined && data.step !== "linkedin" && !ready
  // People appear as the index is built; once there are some the first look takes the right column.
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
      step="setup"
      left={<Checklist run={run} />}
      right={
        // The right column fills in as the stages finish: the page picture while the connections
        // are read, then the first look the moment people are in.
        known > 0 || ready ? (
          <FirstLookPanel
            look={look.data ?? { people: [], total: 0, questions: [] }}
            ready={ready}
            sourcesNext={false}
          />
        ) : data ? (
          <>
            <h2 className="m-0 text-[15px] font-semibold">Your connections page, as I read it</h2>
            <PageShot
              url="/api/install/linkedin-page"
              address="linkedin.com/mynetwork/invite-connect/connections"
              reading={read !== null}
              caption={
                read
                  ? `${read.read.toLocaleString()} of ${read.total.toLocaleString()} read so far`
                  : "Read. Building your network from it now."
              }
            />
          </>
        ) : (
          <Info
            title="While you wait"
            lines={[
              "Your first look: a few people you know and three questions to try",
              "Closing the window does not stop anything",
              "The whole network takes a while; you can use Powerpacks meanwhile",
            ]}
          />
        )
      }
    />
  )
}
