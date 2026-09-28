import { afterEach, describe, expect, it, vi } from "vitest"

import { resultRow } from "@/testing/searches-fixture"

import { copyRows, csvFilename, escapeCsv, escapeHtml, toCsv, toHtmlTable, toPlainText } from "./exports"
import { exportScoreOf } from "./feedback"
import { filterRows, NO_FILTERS, type Score } from "./filters"
import { NO_TAGS } from "./tags"

const OWN = exportScoreOf(new Map())
const HEADERS = ["Name", "Title", "Company", "Location", "Network", "Overall Score", "Reasoning"]
const TODAY = new Date("2026-09-26T12:00:00Z")

// test_browser_overall_filters_export_all_matching_rows_and_tags: 125 people, overall 1–5 in turn.
const SCORED = Array.from({ length: 125 }, (_, index) =>
  resultRow(`score-person-${index}`, `Person ${index}`, {
    overall: (index % 5) + 1,
    reason: `Qualification ${index}`,
  }),
)

/** RFC 4180 records: quoted cells may hold commas, quotes ("") and newlines. */
function parseCsv(text: string): Record<string, string>[] {
  const records: string[][] = [[]]
  let cell = ""
  let quoted = false
  for (let index = 0; index < text.length; index += 1) {
    const char = text.charAt(index)
    const record = records[records.length - 1] ?? []
    if (quoted && char === '"' && text.charAt(index + 1) === '"') {
      cell += '"'
      index += 1
    } else if (char === '"') quoted = !quoted
    else if (!quoted && char === ",") {
      record.push(cell)
      cell = ""
    } else if (!quoted && char === "\n") {
      record.push(cell)
      cell = ""
      records.push([])
    } else cell += char
  }
  records[records.length - 1]?.push(cell)
  const [head = [], ...body] = records
  return body.map((cells) => Object.fromEntries(head.map((name, position) => [name, cells[position] ?? ""])))
}

afterEach(() => vi.unstubAllGlobals())

describe("CSV export", () => {
  it("exports every filtered row, not only the mounted ones (125, then 50)", () => {
    expect(parseCsv(toCsv(filterRows(SCORED, NO_FILTERS, NO_TAGS, OWN), OWN))).toHaveLength(125)
    const filters = { ...NO_FILTERS, scores: new Set<Score>([4, 5]) }
    const exported = parseCsv(toCsv(filterRows(SCORED, filters, NO_TAGS, OWN), OWN))
    expect(exported).toHaveLength(50)
    expect(new Set(exported.map((row) => row["Overall Score"]))).toEqual(new Set(["4", "5"]))
    expect(exported.map((row) => row.Reasoning)).toContain("Qualification 123")
    expect(Object.keys(exported[0] ?? {})).toEqual(HEADERS)
    expect(exported.every((row) => row.Name?.startsWith('=HYPERLINK("https://linkedin.com/'))).toBe(true)
  })

  it("writes the person's own score over the overall, and blank when neither exists", () => {
    const rows = [resultRow("a", "Jordan Bravo", { overall: 4, human: 2 }), resultRow("b", "Casey Delta")]
    expect(parseCsv(toCsv(rows, OWN)).map((row) => row["Overall Score"])).toEqual(["2", ""])
  })

  it("links the name, doubling quotes inside the formula; a name without a profile stays plain", () => {
    const rows = [
      resultRow("a", 'Jordan "JB" Bravo', { linkedin: "https://linkedin.com/in/jordan" }),
      resultRow("b", "Casey Delta", { linkedin: "" }),
    ]
    expect(parseCsv(toCsv(rows, OWN)).map((row) => row.Name)).toEqual([
      '=HYPERLINK("https://linkedin.com/in/jordan","Jordan ""JB"" Bravo")',
      "Casey Delta",
    ])
  })

  it("quotes cells holding a comma, quote or newline", () => {
    expect(escapeCsv("plain")).toBe("plain")
    expect(escapeCsv("Oakland, CA")).toBe('"Oakland, CA"')
    expect(escapeCsv('say "hi"')).toBe('"say ""hi"""')
    expect(escapeCsv("two\nlines")).toBe('"two\nlines"')
  })
})

describe("csvFilename", () => {
  it("is up to five title words without filler, then the date", () => {
    expect(
      csvFilename("Find me the best backend engineers in Oakland for our payments team", [], TODAY),
    ).toBe("best-backend-engineers-oakland-payments_2026-09-26.csv")
  })

  it("leads with the exported people's tags", () => {
    expect(csvFilename("Backend Engineer", ["Backend | Infra"], TODAY)).toBe(
      "backend-infra_backend-engineer_2026-09-26.csv",
    )
    expect(csvFilename("Backend Engineer", ["Pinned", "Call back"], TODAY)).toBe(
      "pinned_call-back_backend-engineer_2026-09-26.csv",
    )
  })

  it("dates the file by the local calendar, not UTC", () => {
    // Just after midnight and just before: one of them crosses the UTC date in any zone but UTC.
    expect(csvFilename("Backend", [], new Date(2026, 8, 27, 0, 30))).toBe("backend_2026-09-27.csv")
    expect(csvFilename("Backend", [], new Date(2026, 8, 27, 23, 30))).toBe("backend_2026-09-27.csv")
  })

  it("falls back to results when no title word is left", () => {
    expect(csvFilename("find the", [], TODAY)).toBe("results_2026-09-26.csv")
  })
})

describe("copy", () => {
  const rows = [
    resultRow("a", "Jordan <B> Bravo", { overall: 5, linkedin: "https://linkedin.com/in/a?x=1&y=2" }),
    resultRow("b", "Casey Delta", { linkedin: "" }),
  ]

  it("escapes HTML", () => {
    expect(escapeHtml(`<a href="x">&</a>`)).toBe("&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;")
  })

  it("builds a table with linked names", () => {
    const html = toHtmlTable(rows, OWN)
    expect(html).toContain(
      `<thead><tr>${HEADERS.map((header) => `<th>${header}</th>`).join("")}</tr></thead>`,
    )
    expect(html).toContain(
      '<td><a href="https://linkedin.com/in/a?x=1&amp;y=2">Jordan &lt;B&gt; Bravo</a></td>',
    )
    expect(html).toContain("<td>Casey Delta</td>")
  })

  it("builds tab-separated text with bare names", () => {
    expect(toPlainText(rows, OWN).split("\n")).toEqual([
      HEADERS.join("\t"),
      [
        "Jordan <B> Bravo",
        "Software Engineer",
        "Example Labs",
        "Oakland, CA",
        "Alex Operator",
        "5",
        "Relevant work",
      ].join("\t"),
      [
        "Casey Delta",
        "Software Engineer",
        "Example Labs",
        "Oakland, CA",
        "Alex Operator",
        "",
        "Relevant work",
      ].join("\t"),
    ])
  })

  it("puts HTML and plain text on the clipboard together", async () => {
    const write = vi.fn((_items: unknown[]) => Promise.resolve())
    vi.stubGlobal("navigator", { clipboard: { write } })
    vi.stubGlobal(
      "ClipboardItem",
      class {
        constructor(readonly items: Record<string, Blob>) {}
      },
    )
    await copyRows(rows, OWN)
    const [item] = write.mock.calls[0]?.[0] ?? []
    expect(item).toMatchObject({ items: { "text/html": expect.any(Blob), "text/plain": expect.any(Blob) } })
  })
})
