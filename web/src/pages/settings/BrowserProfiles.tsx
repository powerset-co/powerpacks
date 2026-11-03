import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { copyChromeProfile, fetchChromeProfiles } from "@/lib/api/accounts"
import { errorText } from "@/lib/api/http"
import { ProfileList } from "@/pages/onboarding/ChromeProfile"

/** The Chrome profile the sign-in browser starts from: the same pick and copy as the onboarding's. */
export function BrowserProfiles() {
  const client = useQueryClient()
  const profiles = useQuery({
    queryKey: ["chrome-profiles"],
    queryFn: ({ signal }) => fetchChromeProfiles(signal),
  })
  const [copying, setCopying] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const use = (folder: string) => {
    setError(null)
    setCopying(folder)
    copyChromeProfile(folder)
      .then(() => client.refetchQueries({ queryKey: ["chrome-profiles"] }))
      .catch((caught: unknown) => setError(errorText(caught)))
      .finally(() => setCopying(null))
  }
  if (profiles.isError) return <p className="m-0 text-xs text-bad">{errorText(profiles.error)}</p>
  if (profiles.data?.profiles.length === 0)
    return <p className="m-0 text-xs">No Chrome profile here is signed in to LinkedIn or Google.</p>
  return (
    <>
      <ProfileList
        profiles={profiles.data?.profiles ?? []}
        copying={copying}
        linked={profiles.data?.linked ?? null}
        onPick={use}
      />
      {error ? <p className="m-0 text-xs text-bad">{error}</p> : null}
    </>
  )
}
