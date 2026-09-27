import { EmptyState, SearchField } from "@/components/shared"
import { Skeleton } from "@/components/ui/skeleton"
import type { SearchCard } from "@/types/searches"

import { SEARCH_PLACEHOLDER } from "./CatalogSearch"
import { CatalogList } from "./CatalogList"

const SKELETON_ROWS = 6

interface SearchSidebarProps {
  cards: readonly SearchCard[] | undefined
  error: Error | null
  selectedId: string | null
  onOpen: (runId: string) => void
}

// The left column: every saved search, searched and grouped (ConversationSidebar's list).
export function SearchSidebar({ cards, error, selectedId, onOpen }: SearchSidebarProps) {
  if (cards?.length) return <CatalogList cards={cards} selectedId={selectedId} onOpen={onOpen} />
  if (cards) {
    return (
      <EmptyState className="my-8 px-4 text-[12.5px]" data-catalog-empty>
        <b className="mb-1 block text-foreground">No completed searches</b>
        New searches will appear here when they have CE scores.
      </EmptyState>
    )
  }
  if (error) {
    return (
      <EmptyState className="my-8 px-4 text-[12.5px]" data-catalog-empty>
        {error.message}
      </EmptyState>
    )
  }
  return (
    <div className="rail-top" aria-busy="true">
      <SearchField
        className="w-full"
        placeholder={SEARCH_PLACEHOLDER}
        aria-label="Search saved searches"
        disabled
      />
      <span className="sr-only">Loading searches…</span>
      {Array.from({ length: SKELETON_ROWS }, (_, index) => (
        <Skeleton key={index} className="h-[58px]" />
      ))}
    </div>
  )
}
