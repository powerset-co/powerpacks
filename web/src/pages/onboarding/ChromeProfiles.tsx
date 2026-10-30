import { useQuery } from "@tanstack/react-query"
import { useState } from "react"

import { Spinner } from "@/components/shared"
import { fetchChromeProfiles, copyChromeProfile, type ChromeProfile } from "@/lib/api/accounts"
import { errorText } from "@/lib/api/http"

import { Note } from "./Frame"

const sessions = (profile: ChromeProfile) =>
  [profile.linkedin && "LinkedIn", profile.google && "Google"].filter(Boolean).join(" and ")

/** The user's Chrome profiles that are signed in somewhere. Picking one brings its sign-ins and saved
 *  passwords along, so the Connect button below is only for someone with none to pick. */
export function ChromeProfiles({ onUsed }: { onUsed: () => void }) {
  const profiles = useQuery({ queryKey: ["chrome-profiles"], queryFn: ({ signal }) => fetchChromeProfiles(signal) })
  const [copying, setCopying] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  if (!profiles.data?.length) return null

  const use = (folder: string) => {
    setError(null)
    setCopying(folder)
    copyChromeProfile(folder)
      .then(onUsed)
      .catch((caught: unknown) => setError(errorText(caught)))
      .finally(() => setCopying(null))
  }

  return (
    <div className="flex flex-col gap-2">
      <Note>Use a Chrome profile to skip signing in. Its logins and saved passwords are copied on this Mac only.</Note>
      <ul className="m-0 flex max-h-56 list-none flex-col gap-1.5 overflow-y-auto p-0">
        {profiles.data.map((profile) => (
          <li key={profile.folder}>
            <button
              type="button"
              disabled={copying !== null}
              onClick={() => use(profile.folder)}
              className="flex w-full items-center justify-between gap-3 rounded-md border border-line bg-transparent px-3 py-2 text-left hover:bg-muted disabled:opacity-60"
            >
              <span className="min-w-0">
                <span className="block truncate text-sm font-medium">{profile.email || profile.name}</span>
                <span className="block text-xs text-muted-foreground">Signed in to {sessions(profile)}</span>
              </span>
              {copying === profile.folder ? <Spinner /> : null}
            </button>
          </li>
        ))}
      </ul>
      {error ? <Note bad>{error}</Note> : null}
    </div>
  )
}
