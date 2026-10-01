import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { useState, type ReactNode } from "react"
import { MemoryRouter } from "react-router-dom"

import { ReviewContext, type Review } from "@/pages/review/hooks/useReview"

interface ReviewHarnessProps {
  /** The page's `Review` as the piece under test sees it (`fakeReview()` in review-fixture.ts). */
  review: Review
  /** The address the piece renders at. */
  path?: string
  children: ReactNode
}

// What a review stage or shared component needs around it in a test: a query client (no
// retries), a router, and the page's `Review`.
export function ReviewHarness({ review, path = "/", children }: ReviewHarnessProps) {
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }))
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <ReviewContext.Provider value={review}>{children}</ReviewContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>
  )
}
