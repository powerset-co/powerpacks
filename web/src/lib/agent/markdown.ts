// The Markdown blocks Codex writes (paragraphs, headings, lists, fenced code, tables), split in
// one pass, first rule wins. pages/agent/Markdown renders them.

export type Block =
  | { kind: "paragraph"; text: string }
  | { kind: "heading"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "code"; text: string }
  | { kind: "table"; head: string[]; rows: string[][] }

const FENCE = "```"
const HEADING = /^#{1,6}\s+/
const BULLET = /^\s*[-*•]\s+/
const NUMBERED = /^\s*\d+[.)]\s+/
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/

function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\||\|$/g, "")
    .split("|")
    .map((cell) => cell.trim())
}

function isTableStart(lines: string[], at: number): boolean {
  return (lines[at] ?? "").includes("|") && TABLE_RULE.test(lines[at + 1] ?? "")
}

/** Splits the text into blocks, one pass, first rule wins. */
export function blocks(source: string): Block[] {
  const lines = source.replace(/\r\n/g, "\n").split("\n")
  const out: Block[] = []
  let at = 0
  while (at < lines.length) {
    const line = lines[at] ?? ""
    if (!line.trim()) {
      at += 1
      continue
    }
    if (line.startsWith(FENCE)) {
      const body: string[] = []
      at += 1
      while (at < lines.length && !(lines[at] ?? "").startsWith(FENCE)) body.push(lines[at++] ?? "")
      at += 1
      out.push({ kind: "code", text: body.join("\n") })
      continue
    }
    if (HEADING.test(line)) {
      out.push({ kind: "heading", text: line.replace(HEADING, "") })
      at += 1
      continue
    }
    if (isTableStart(lines, at)) {
      const head = cells(line)
      at += 2
      const rows: string[][] = []
      while (at < lines.length && (lines[at] ?? "").includes("|")) rows.push(cells(lines[at++] ?? ""))
      out.push({ kind: "table", head, rows })
      continue
    }
    const marker = BULLET.test(line) ? BULLET : NUMBERED.test(line) ? NUMBERED : null
    if (marker) {
      const items: string[] = []
      while (at < lines.length && marker.test(lines[at] ?? ""))
        items.push((lines[at++] ?? "").replace(marker, ""))
      out.push({ kind: "list", ordered: marker === NUMBERED, items })
      continue
    }
    const paragraph: string[] = []
    while (at < lines.length) {
      const next = lines[at] ?? ""
      if (
        !next.trim() ||
        next.startsWith(FENCE) ||
        HEADING.test(next) ||
        BULLET.test(next) ||
        NUMBERED.test(next) ||
        isTableStart(lines, at)
      )
        break
      paragraph.push(next)
      at += 1
    }
    out.push({ kind: "paragraph", text: paragraph.join("\n") })
  }
  return out
}
