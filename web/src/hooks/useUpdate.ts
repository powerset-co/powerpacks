import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { checkUpdate, lastCheck, UPDATE_INTERVAL_MS, type Update } from "@/lib/api/update"
import { isDesktop } from "@/lib/desktop"

const UPDATE_KEY = ["update"]

export function useUpdate(): Update | null {
  const [checked] = useState(lastCheck)
  const query = useQuery({
    queryKey: UPDATE_KEY,
    queryFn: checkUpdate,
    enabled: isDesktop(),
    initialData: checked?.update,
    initialDataUpdatedAt: checked?.at,
    staleTime: UPDATE_INTERVAL_MS,
    refetchInterval: UPDATE_INTERVAL_MS,
    refetchIntervalInBackground: true,
    retry: false,
  })
  return query.data ?? null
}
