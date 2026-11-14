import { useQuery } from "@tanstack/react-query"
import { useState } from "react"
import { Navigate, useNavigate } from "react-router-dom"

import braveIcon from "@/assets/browsers/brave.svg?inline"
import chromeIcon from "@/assets/browsers/chrome.svg?inline"
import { CheckIcon, Spinner } from "@/components/shared"
import { copyChromeProfile, fetchChromeProfiles, type ChromeProfile as Profile } from "@/lib/api/accounts"
import { errorText } from "@/lib/api/http"

import { Frame, Heading, Note } from "./Frame"
import { Guide } from "./guide/Guide"
import { SAMPLE_PERSON } from "./guide/demos"
import { SIGNUP } from "./steps"

const ICONS = { chrome: chromeIcon, brave: braveIcon }
/** A profile's id across browsers: two browsers both have a "Default". */
const idOf = (profile: Pick<Profile, "browser" | "folder"> | null) =>
  profile ? `${profile.browser}/${profile.folder}` : null

const sessions = (profile: Profile) =>
  [profile.linkedin && "LinkedIn", profile.google && "Google"].filter(Boolean).join(" and ")

/** Chrome: which of the user's Chrome profiles to start from, before any sign-in, so the sign-ins
 *  that follow find their logins and saved passwords. Someone with none to pick never sees it. */
export function ChromeProfile() {
  const navigate = useNavigate()
  const profiles = useQuery({
    queryKey: ["chrome-profiles"],
    queryFn: ({ signal }) => fetchChromeProfiles(signal),
  })
  const [copying, setCopying] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  if (profiles.data?.profiles.length === 0 || profiles.isError) return <Navigate to={SIGNUP.href} replace />

  const use = (profile: Profile) => {
    setError(null)
    setCopying(idOf(profile))
    copyChromeProfile(profile)
      .then(() => void navigate(SIGNUP.href))
      .catch((caught: unknown) => {
        setError(errorText(caught))
        setCopying(null)
      })
  }
  const first = profiles.data?.profiles[0]

  return (
    <Frame
      step="chrome"
      left={
        <>
          <Heading
            title="Start from your Chrome"
            line="Pick the profile you use for LinkedIn and Google. A copy stays on this Mac so Powerpacks can sync in the background."
          />
          <ProfileList profiles={profiles.data?.profiles ?? []} copying={copying} onPick={use} />
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

/** The user's Chrome and Brave profiles as buttons, each with its browser's icon, the one the
 *  sign-in browser was copied from checked. */
export function ProfileList({
  profiles,
  copying,
  linked = null,
  onPick,
}: {
  profiles: Profile[]
  copying: string | null
  linked?: Pick<Profile, "browser" | "folder"> | null
  onPick: (profile: Profile) => void
}) {
  return (
    <ul className="m-0 flex max-h-72 list-none flex-col gap-1.5 overflow-y-auto p-0">
      {profiles.map((profile) => (
        <li key={idOf(profile)}>
          <button
            type="button"
            disabled={copying !== null}
            aria-pressed={idOf(linked) === idOf(profile)}
            onClick={() => onPick(profile)}
            className="flex w-full cursor-pointer items-center justify-between gap-3 rounded-md border border-line bg-transparent px-3 py-2 text-left hover:bg-surface-2 disabled:opacity-60"
          >
            <img src={ICONS[profile.browser]} alt={profile.browser === "brave" ? "Brave" : "Chrome"} className="size-4 shrink-0" />
            <span className="mr-auto min-w-0">
              <span className="block truncate text-sm font-medium">{profile.email || profile.name}</span>
              <span className="block text-xs text-muted-foreground">Signed in to {sessions(profile)}</span>
            </span>
            {copying === idOf(profile) ? (
              <Spinner />
            ) : idOf(linked) === idOf(profile) ? (
              <CheckIcon className="size-3.5 text-ok" />
            ) : null}
          </button>
        </li>
      ))}
    </ul>
  )
}
