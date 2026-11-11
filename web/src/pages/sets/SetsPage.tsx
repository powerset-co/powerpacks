import { EmptyState, Page } from "@/components/shared"
import { offeredSets, setName, useSearchScope } from "@/lib/agent/useSearchScope"
import { errorText } from "@/lib/api/http"
import { PageTable } from "@/pages/companies/parts"
import { CARD } from "@/pages/companies/table"

interface Row {
  key: string
  name: string
  people: string
}

// Where you can search, as the Search-in picker offers it: this Mac's local index first, then your
// Powerset sets, without the cloud copy of your own network when the local index holds it.
export function SetsPage() {
  const scope = useSearchScope()
  const rows: Row[] = scope.data
    ? [
        ...(scope.data.local_db ? [{ key: "local", name: "Local index", people: "–" }] : []),
        ...offeredSets(scope.data).map((set) => ({
          key: set.set_id,
          name: setName(set),
          people: set.person_count.toLocaleString(),
        })),
      ]
    : []

  return (
    <Page title="Sets">
      {scope.error ? <p className="m-0 text-[13px] text-bad">{errorText(scope.error)}</p> : null}
      {scope.isPending ? <EmptyState>Loading…</EmptyState> : null}
      {scope.data ? (
        <section className={CARD} aria-label="Sets">
          {rows.length ? (
            <PageTable
              label="Sets"
              columns={[{ label: "Set" }, { label: "People", className: "text-right tabular-nums" }]}
              grid="grid-cols-[minmax(0,1fr)_96px]"
              items={rows}
              getKey={(row) => row.key}
              cells={(row) => [
                <span key="name" className="break-words font-semibold">
                  {row.name}
                </span>,
                row.people,
              ]}
            />
          ) : (
            <EmptyState className="my-6">No sets yet.</EmptyState>
          )}
        </section>
      ) : null}
    </Page>
  )
}
