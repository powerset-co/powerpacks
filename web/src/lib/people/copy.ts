// The page's words for machine values, ported 1:1 from the legacy people.js.

import type { LastUpload, UploadPlan } from "@/lib/api/upload"
import { CHANNELS } from "@/lib/channels"
import { parseDate, plural } from "@/lib/copy"
import type { CalmPhase, UploadPhase } from "@/lib/people/upload"

// Brand casing, keyed lowercase: the channel titles plus JEV.
const BRANDS: Readonly<Record<string, string>> = {
  ...Object.fromEntries(Object.entries(CHANNELS).map(([channel, { title }]) => [channel, title])),
  jev: "JEV",
}

export type TextKind =
  | "share"
  | "reason"
  | "worth"
  | "source"
  | "mode"
  | "hierarchy"
  | "intro_source"
  | "seniority"
  | "function"
  | "direction"
  | "cadence"
  | "labels"

// Plain words for the machine values; anything unmapped falls back to sentence case.
export const TEXT: Readonly<Record<TextKind, Readonly<Record<string, string>>>> = {
  share: { yes: "Sharing", confirm: "Needs confirmation", no: "Not sharing" },
  reason: {
    worth_yes: "Worth: yes",
    worth_maybe: "Worth: maybe",
    worth_no: "Worth: no",
    owner: "You (the owner)",
    human_share: "You chose to share",
    human_private: "You chose to keep private",
    family: "Family",
    romantic_partner: "Partner",
    minor: "Minor",
    sensitive_context: "Sensitive context",
    sensitive_provider: "Clinician, lawyer, or banker",
    automated_sender: "Automated sender",
    stranger: "Stranger",
  },
  worth: { yes: "Yes", maybe: "Maybe", no: "No", unjudged: "Not assessed" },
  source: { human: "You", machine: "AI" },
  mode: { professional_only: "Work only", personal_only: "Personal only", mixed: "Work and personal" },
  hierarchy: {
    manager: "They managed you",
    peer: "Same level",
    report: "You managed them",
    none: "No reporting line",
  },
  intro_source: { mutual_friend: "Mutual friend", cold_outreach: "Unsolicited contact" },
  seniority: { mid: "Mid-level" },
  function: { founder_exec: "Founder or executive", people: "People and recruiting" },
  direction: { they_initiate: "Mostly them", i_initiate: "Mostly you", mutual: "Both" },
  cadence: { dormant: "No contact in over 2 years", stale: "No contact in over 1 year" },
  labels: {
    is_coworker_current: "Current colleague",
    is_coworker_past: "Former colleague",
    is_vendor_or_partner: "Vendor or partner",
    is_mentor_or_advisor: "Mentor or adviser",
    is_mentee_or_report: "Someone you mentor or manage",
    is_neighbor_or_local: "Neighbor or local contact",
    is_healthcare_legal_or_financial_provider: "Healthcare, legal, or financial provider",
    owner_would_intro: "You would introduce them",
    they_would_take_owner_call: "They would take your call",
    notable: "Publicly notable",
  },
}

export const SEARCH_PLACEHOLDER = "Search name, title, company, location"
export const SEARCH_LABEL = "Search people"

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

export function humanize(value: string | null | undefined): string {
  return (value ?? "").replace(/^is_/, "").replaceAll("_", " ").trim()
}

/** Sentence case, brand names kept: "close friend" -> "Close friend", "imessage" -> "iMessage". */
export function sentence(value: string | null | undefined): string {
  const words = humanize(value).split(" ").filter(Boolean)
  const [first] = words
  if (first === undefined) return ""
  const cased = words.map((word) => BRANDS[word.toLowerCase()] ?? word)
  if (!BRANDS[first.toLowerCase()]) cased[0] = first.charAt(0).toUpperCase() + first.slice(1)
  return cased.join(" ")
}

export function label(kind: TextKind, value: string): string {
  return TEXT[kind][value] ?? sentence(value)
}

/** ISO prefixes in a fact date to words: "2012-02 to 2012-03" -> "Feb 2012 to Mar 2012". */
export function eventDate(value: string | null | undefined): string {
  return (value ?? "").replace(
    /\b(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?\b/g,
    (_match: string, year: string, month?: string, day?: string) => {
      const name = month ? (MONTHS[Number(month) - 1] ?? "") : ""
      if (day) return `${name} ${Number(day)}, ${year}`
      return name ? `${name} ${year}` : year
    },
  )
}

// The share dialog and its trigger.
export const UPLOAD = {
  share: "Share network",
  shareChanges: "Share changes",
  view: "View upload",
  neverShared: "Never shared",
  check: "Check network",
  checkAgain: "Check again",
  confirm: "Confirm sharing",
  resume: "Resume sharing",
  close: "Close",
  starting: "Starting…",
  reconnecting: "Reconnecting…",
  progress: "People uploaded",
  uploaded: "Uploaded",
  upToDate: "Already up to date",
  titleUpToDate: "Your network is up to date",
} as const

export const UPLOAD_TITLE: Readonly<Record<UploadPhase, string>> = {
  idle: "Share your network",
  checking: "Checking your network",
  ready: "Ready to share",
  uploading: "Uploading your network",
  completed: "Your network is shared",
  "check-failed": "Check failed",
  "upload-failed": "Upload failed",
  interrupted: "Upload interrupted",
  refused: "Upload not started",
}

// A failed phase shows the server's sentence instead.
export const UPLOAD_SENTENCE: Readonly<Record<CalmPhase, string>> = {
  idle: "Only people marked Share are uploaded.",
  checking: "Counting who will upload. Nothing is shared yet.",
  ready: "Only people marked Share with a LinkedIn profile upload.",
  uploading: "Closing keeps the upload running.",
  completed: "Only people marked Share are uploaded.",
}

// The check's plan, in order; a zero row is hidden except what will upload.
export const PLAN_ROWS: readonly { key: keyof UploadPlan; label: string }[] = [
  { key: "marked_share", label: "Marked Share" },
  { key: "with_linkedin", label: "With LinkedIn, will upload" },
  { key: "without_linkedin", label: "Without LinkedIn, stay local" },
  { key: "new_to_cloud", label: "New to the cloud" },
  { key: "changed", label: "Changed" },
  { key: "already_shared", label: "Already shared" },
  { key: "losing_access", label: "Losing access" },
  { key: "companies_missing", label: "Companies missing" },
]
export const PLAN_ALWAYS: keyof UploadPlan = "with_linkedin"

const LAST_UPLOAD: Readonly<Record<LastUpload["status"], (last: LastUpload) => string>> = {
  completed: (last) => `Shared ${(last.uploaded + last.skipped).toLocaleString()}`,
  failed: () => "Upload failed",
  interrupted: () => "Upload interrupted",
}

/** "Sep 27"; an unparseable value as written. */
export function dayMonth(value: string): string {
  const date = parseDate(value)
  return date ? date.toLocaleDateString("en-US", { month: "short", day: "numeric" }) : value
}

/** "Finished Sep 27, 3:04 PM". */
export function finishedAt(value: string): string {
  const date = parseDate(value)
  const when = date
    ? date.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    : value
  return `Finished ${when}`
}

/** The trigger's muted line: "Shared 128 · Sep 27", "Upload interrupted · Sep 27", "Never shared". */
export function lastUploadLine(last: LastUpload | null): string {
  if (!last) return UPLOAD.neverShared
  return `${LAST_UPLOAD[last.status](last)} · ${dayMonth(last.finished_at)}`
}

/** The page toast when an upload finishes while the dialog is closed. */
export function sharedToast(count: number): string {
  return `Shared ${plural(count, "person")}.`
}
