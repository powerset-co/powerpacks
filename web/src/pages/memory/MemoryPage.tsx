import { useQuery } from "@tanstack/react-query"

import { EmptyState, Page } from "@/components/shared"
import { errorText } from "@/lib/api/http"
import { fetchMemory, MEMORY_KEY } from "@/lib/api/memory"

import { Details } from "./Details"
import { Enabled } from "./Enabled"
import { FirstRun } from "./FirstRun"
import { Running } from "./Running"

/** The local memory directory: enable it once, watch the run, then update and search it. The run
 *  lives in the server's job record (packs/memory/job.py); the page only polls it, every 2 s while
 *  a run works. */
export function MemoryPage() {
  const memory = useQuery({
    queryKey: MEMORY_KEY,
    queryFn: ({ signal }) => fetchMemory(signal),
    refetchInterval: (query) => (query.state.data?.status === "running" ? 2_000 : false),
  })
  const record = memory.data
  return (
    <Page title="Memory">
      {!record ? (
        <EmptyState role={memory.error ? "alert" : undefined}>
          {memory.error ? errorText(memory.error) : "Reading your network…"}
        </EmptyState>
      ) : record.status === "running" ? (
        <Running record={record} />
      ) : record.enabled ? (
        <Enabled record={record} />
      ) : (
        <FirstRun record={record} />
      )}
      {record?.enabled ? <Details record={record} /> : null}
    </Page>
  )
}
