import { DOSSIER } from "@/lib/review/copy"
import { cn } from "@/lib/utils"

import { useDossier } from "../hooks/useDossier"

interface DossierProps {
  /** The parent's slug. */
  slug: string
  className?: string
}

// The lazy dossier: mounting it asks for the person's dossier once. "Loading…" until the
// answer; the server's HTML; "No details found" when the server has none; "Could not load
// details" when the request fails.
export function Dossier({ slug, className }: DossierProps) {
  const dossier = useDossier(slug)
  const classes = cn("dossier-text", className)
  if (dossier.status === "ready") {
    // The HTML is this machine's own server rendering the person's own markdown file.
    return <div className={classes} dangerouslySetInnerHTML={{ __html: dossier.html }} />
  }
  return (
    <div className={classes} aria-busy={dossier.status === "loading" || undefined}>
      {DOSSIER[dossier.status]}
    </div>
  )
}
