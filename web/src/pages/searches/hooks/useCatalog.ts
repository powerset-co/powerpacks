import { useQuery } from "@tanstack/react-query"

import { fetchCatalog } from "@/lib/api/searches"

/** The saved searches, newest first (the server's order). */
export function useCatalog() {
  return useQuery({
    queryKey: ["searches", "catalog"],
    queryFn: async ({ signal }) => (await fetchCatalog(signal)).searches,
  })
}
