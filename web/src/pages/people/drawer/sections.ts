// The drawer's sections in page order, and the props each section shell takes.

export type SectionKey = "decision" | "timeline" | "facts" | "relationship" | "topics" | "dossier" | "contact" | "confidence";

export const OPEN_BY_DEFAULT: ReadonlySet<SectionKey> = new Set(["decision", "timeline"]);

export interface SectionProps {
  open: boolean;
  onToggle: (open: boolean) => void;
}

export type SectionState = (key: SectionKey) => SectionProps;
