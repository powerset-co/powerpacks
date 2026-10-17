// The drawer's sections in page order, and the props each section shell takes.

export type SectionKey =
  "facts" | "timeline" | "relationship" | "decision" | "topics" | "dossier" | "confidence"

export const OPEN_BY_DEFAULT: ReadonlySet<SectionKey> = new Set(["facts"])

export interface SectionProps {
  open: boolean
  onToggle: (open: boolean) => void
}

export type SectionState = (key: SectionKey) => SectionProps
