// CSV download and clipboard copy of the filtered results (results.js exportResults,
// copyResults). Callers pass every filtered row, never only the mounted ones.

import { exportScore, type ToolbarRow } from "./filters"

interface ExportColumn {
  header: string
  // The CSV cell; also the copied cell unless `text` / `html` say otherwise.
  value: (row: ToolbarRow) => string
  text?: (row: ToolbarRow) => string
  html?: (row: ToolbarRow) => string
}

const FILLER_WORDS = new Set([
  "a",
  "an",
  "the",
  "in",
  "at",
  "on",
  "for",
  "to",
  "of",
  "and",
  "or",
  "with",
  "who",
  "are",
  "is",
  "that",
  "from",
  "by",
  "as",
  "my",
  "our",
  "find",
  "search",
  "looking",
  "look",
  "get",
  "me",
  "i",
  "want",
])
const FILENAME_WORDS = 5

function quoteFormula(value: string): string {
  return value.replaceAll('"', '""')
}

// The CSV's name cell links out in a spreadsheet; a rich paste gets an anchor instead.
function spreadsheetName(row: ToolbarRow): string {
  const { name, linkedin_url: url } = row.row
  return url ? `=HYPERLINK("${quoteFormula(url)}","${quoteFormula(name)}")` : name
}

function linkedName(row: ToolbarRow): string {
  const { name, linkedin_url: url } = row.row
  return url ? `<a href="${escapeHtml(url)}">${escapeHtml(name)}</a>` : escapeHtml(name)
}

export const EXPORT_COLUMNS: readonly ExportColumn[] = [
  { header: "Name", value: spreadsheetName, text: (row) => row.row.name, html: linkedName },
  { header: "Title", value: (row) => row.row.title },
  { header: "Company", value: (row) => row.row.company },
  { header: "Location", value: (row) => row.row.location },
  { header: "Network", value: (row) => row.row.source_operator },
  { header: "Overall Score", value: (row) => String(exportScore(row) ?? "") },
  { header: "Reasoning", value: (row) => row.reason },
]

export function escapeCsv(value: string): string {
  return /[",\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value
}

export function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
}

export function toCsv(
  rows: readonly ToolbarRow[],
  columns: readonly ExportColumn[] = EXPORT_COLUMNS,
): string {
  const lines = [
    columns.map((column) => column.header),
    ...rows.map((row) => columns.map((column) => column.value(row))),
  ]
  return lines.map((cells) => cells.map(escapeCsv).join(",")).join("\n")
}

function htmlCell(row: ToolbarRow, column: ExportColumn): string {
  return `<td>${column.html ? column.html(row) : escapeHtml(column.value(row))}</td>`
}

export function toHtmlTable(
  rows: readonly ToolbarRow[],
  columns: readonly ExportColumn[] = EXPORT_COLUMNS,
): string {
  const head = columns.map((column) => `<th>${escapeHtml(column.header)}</th>`).join("")
  const body = rows
    .map((row) => `<tr>${columns.map((column) => htmlCell(row, column)).join("")}</tr>`)
    .join("")
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`
}

/** Tab-separated, the bare name in the first column: what a plain-text paste gets. */
export function toPlainText(
  rows: readonly ToolbarRow[],
  columns: readonly ExportColumn[] = EXPORT_COLUMNS,
): string {
  const cell = (row: ToolbarRow, column: ExportColumn) => (column.text ?? column.value)(row)
  const lines = [
    columns.map((column) => column.header),
    ...rows.map((row) => columns.map((column) => cell(row, column))),
  ]
  return lines.map((cells) => cells.join("\t")).join("\n")
}

/** "<tags>_<up to five title words>_<yyyy-mm-dd>.csv", filler words dropped. */
export function csvFilename(title: string, tags: readonly string[], today: Date = new Date()): string {
  const words = title
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, "")
    .split(/\s+/)
    .filter((word) => word && !FILLER_WORDS.has(word))
    .slice(0, FILENAME_WORDS)
  const prefix = tags
    .map((tag) =>
      tag
        .toLowerCase()
        .replace(/[^\p{L}\p{N}]+/gu, "-")
        .replace(/^-|-$/g, ""),
    )
    .filter(Boolean)
    .join("_")
  const stem = words.join("-") || "results"
  return `${prefix ? `${prefix}_` : ""}${stem}_${today.toISOString().slice(0, 10)}.csv`
}

export function downloadCsv(rows: readonly ToolbarRow[], filename: string): void {
  const url = URL.createObjectURL(new Blob([toCsv(rows)], { type: "text/csv;charset=utf-8;" }))
  const link = document.createElement("a")
  link.href = url
  link.download = filename
  document.body.append(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

/** Rich (a table with linked names) and plain (tab-separated) on the clipboard at once. */
export async function copyRows(rows: readonly ToolbarRow[]): Promise<void> {
  await navigator.clipboard.write([
    new ClipboardItem({
      "text/html": new Blob([toHtmlTable(rows)], { type: "text/html" }),
      "text/plain": new Blob([toPlainText(rows)], { type: "text/plain" }),
    }),
  ])
}
