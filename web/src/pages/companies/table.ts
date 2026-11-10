// The Companies page's card face and cell text.

import type { Company } from "@/types/companies"

// The page's one card face, so the search, the list, the company and its people match.
export const CARD = "rounded-[var(--radius-m)] border border-line bg-card shadow-[var(--shadow-1)]"
// Narrow windows (390 px) drop the secondary columns rather than scroll sideways.
export const WIDE_ONLY = "max-[680px]:hidden"

export function location(company: Company): string {
  return [company.city, company.state, company.country].filter(Boolean).join(", ")
}

export function count(value: number | null | undefined): string {
  return value == null ? "–" : value.toLocaleString()
}
