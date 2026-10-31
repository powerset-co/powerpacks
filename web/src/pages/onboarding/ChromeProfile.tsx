import { useQuery } from "@tanstack/react-query"
import { useState } from "react"
import { Navigate, useNavigate } from "react-router-dom"

import { Spinner } from "@/components/shared"
import { copyChromeProfile, fetchChromeProfiles, type ChromeProfile as Profile } from "@/lib/api/accounts"
import { errorText } from "@/lib/api/http"

import { Frame, Heading, Note } from "./Frame"
import { Guide } from "./guide/Guide"
import { SAMPLE_PERSON } from "./guide/demos"
import { SIGNUP } from "./steps"

const sessions = (profile: Profile) =>
  [profile.linkedin && "LinkedIn", profile.google && "Google"].filter(Boolean).join(" and ")

/** Chrome: which of the user's Chrome profiles to start from, before any sign-in, so the sign-ins
 *  that follow find their logins and saved passwords. Someone with none to pick never sees it. */
export function ChromeProfile() {
  const navigate = useNavigate()
  const profiles = useQuery({ queryKey: ["chrome-profiles"], queryFn: ({ signal }) => fetchChromeProfiles(signal) })
  const [copying, setCopying] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  if (profiles.data?.length === 0 || profiles.isError) return <Navigate to={SIGNUP.href} replace />

  const use = (folder: string) => {
    setError(null)
    setCopying(folder)
    copyChromeProfile(folder)
      .then(() => void navigate(SIGNUP.href))
      .catch((caught: unknown) => {
        setError(errorText(caught))
        setCopying(null)
      })
  }
  const first = profiles.data?.[0]

  return (
    <Frame
      step="chrome"
      left={
        <>
          <Heading title="Start from your Chrome" line="Pick the profile you use for LinkedIn and Google. A copy stays on this Mac so Powerpacks can sync in the background." />
          <ul className="m-0 flex max-h-72 list-none flex-col gap-1.5 overflow-y-auto p-0">
            {profiles.data?.map((profile) => (
              <li key={profile.folder}>
                <button
                  type="button"
                  disabled={copying !== null}
                  onClick={() => use(profile.folder)}
                  className="flex w-full cursor-pointer items-center justify-between gap-3 rounded-md border border-line bg-transparent px-3 py-2 text-left hover:bg-surface-2 disabled:opacity-60"
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
          <button
            type="button"
            disabled={copying !== null}
            onClick={() => void navigate(SIGNUP.href)}
            className="cursor-pointer self-start border-0 bg-transparent p-0 text-xs text-muted-foreground hover:text-foreground"
          >
            Start empty and sign in myself
          </button>
        </>
      }
      right={
        <Guide
          source="chrome"
          person={first?.email ? { name: first.email, email: first.email } : SAMPLE_PERSON}
        />
      }
    />
  )
}
