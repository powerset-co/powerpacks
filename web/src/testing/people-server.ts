// A stand-in for GET /api/people/page in page tests: the fixture's rows through the view's tab, hide,
// search, relationship filter, name sort and slice (the server's SQL, model.py), with the worth calls
// and deletes the test made applied, so a refetch after a write reads what the write left.

import type { PeoplePagePayload, PeoplePayload, PersonCell } from "@/types/people"

const written = new Map<string, string>()
const deleted = new Set<string>()

/** Forget the writes of the last test. */
export function resetPeopleServer(): void {
  written.clear()
  deleted.clear()
}

/** A worth call the test sent, as the server would store it. */
export function writeWorthOnServer(ids: readonly string[], worth: string): void {
  for (const id of ids) written.set(id, worth)
}

/** A delete the test sent. */
export function deleteOnServer(ids: readonly string[]): void {
  for (const id of ids) deleted.add(id)
}

export function pageResponse(url: string, payload: PeoplePayload): Response {
  const params = new URL(url, "http://local").searchParams
  const at = (name: string) => payload.columns.indexOf(name)
  const cell = (row: PersonCell[], name: string) => row[at(name)]
  const rows = payload.rows
    .filter((row) => !deleted.has(String(cell(row, "parent_id"))))
    .map((row) => {
      const worth = written.get(String(cell(row, "parent_id")))
      if (worth === undefined) return row
      const next = [...row]
      next[at("worth")] = worth
      next[at("worth_source")] = "human"
      next[at("reason")] = "human_worth"
      return next
    })
  // model.py DECISION: an undecided LinkedIn-only person is a Keep.
  const tabOf = (row: PersonCell[]) =>
    String(cell(row, "worth")) || (cell(row, "linkedin_only") ? "yes" : "unjudged")
  const tab = params.get("tab") ?? "all"
  const inTab = rows.filter((row) => tab === "all" || tabOf(row) === tab)
  // model.py page(): a hide left alone steps aside when it would hide everyone; one turned on never does.
  const hide =
    !params.getAll("show").includes("linkedin_only") &&
    (params.getAll("hide").includes("linkedin_only") || rows.some((row) => !cell(row, "linkedin_only")))
  const text = (params.get("q") ?? "").trim().toLowerCase()
  const kinds = params.getAll("filter.relationship_kind")
  const id = params.get("id")
  const matching = inTab
    .filter((row) => !hide || !cell(row, "linkedin_only"))
    .filter((row) => !id || cell(row, "parent_id") === id)
    .filter((row) => !kinds.length || kinds.includes(String(cell(row, "relationship_kind"))))
    .filter(
      (row) =>
        !text ||
        ["name", "title", "company", "location"]
          .map((name) => String(cell(row, name)))
          .join(" ")
          .toLowerCase()
          .includes(text),
    )
    .sort((a, b) => String(cell(a, "name")).localeCompare(String(cell(b, "name"))))
  const offset = Number(params.get("offset") ?? 0)
  const limit = Number(params.get("limit") ?? 200)
  const count = (pick: (row: PersonCell[]) => string, among: PersonCell[][]) =>
    among.reduce<Record<string, number>>((counts, row) => {
      const key = pick(row)
      if (key) counts[key] = (counts[key] ?? 0) + 1
      return counts
    }, {})
  const body: PeoplePagePayload = {
    matching: matching.length,
    totals: count(tabOf, rows),
    kinds: count((row) => String(cell(row, "relationship_kind")), inTab),
    columns: payload.columns,
    rows: params.get("ids") === "1" ? [] : matching.slice(offset, offset + limit),
    ids: params.get("ids") === "1" ? matching.map((row) => String(cell(row, "parent_id"))) : [],
    hidden: hide ? ["linkedin_only"] : [],
  }
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })
}
