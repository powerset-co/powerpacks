import { LinkedInIcon } from "@/components/shared/icons/channels"
import type { PondCandidate } from "@/types/searches"

// The LinkedIn mark before a person's headline (the row, the drawer); nothing without a profile.
export function LinkedInLink({ row }: { row: Pick<PondCandidate, "name" | "linkedin_url"> }) {
  if (!row.linkedin_url) return null
  return (
    <a
      className="result-linkedin"
      href={row.linkedin_url}
      target="_blank"
      rel="noreferrer"
      aria-label={`${row.name} on LinkedIn`}
    >
      <LinkedInIcon />
    </a>
  )
}
