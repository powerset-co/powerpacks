import { useQuery } from "@tanstack/react-query";

import { fetchPeople } from "@/lib/api/people";
import type { Person } from "@/types/people";

export const PEOPLE_QUERY_KEY = ["people"] as const;

/** Every person once; loaded on open, patched in place by writes, never refetched on focus. */
export function usePeopleQuery() {
  return useQuery<Person[], Error>({
    queryKey: PEOPLE_QUERY_KEY,
    queryFn: fetchPeople,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
    retry: false,
  });
}
