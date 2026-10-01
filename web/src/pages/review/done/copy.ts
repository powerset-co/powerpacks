// The Done stage's words (server.py `full_page`, the "All set" panel).

export const ALL_SET = "All set"

/** What the review came to: the LinkedIn identities checked and the people rejected. */
export function reviewTally(checked: number, rejected: number): string {
  return `${checked} identities checked · ${rejected} rejected`
}
