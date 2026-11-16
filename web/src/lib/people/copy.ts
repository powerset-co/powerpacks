// The page's words for machine values, ported 1:1 from the legacy people.js.

import type { ChannelCoverage, LogbookResult } from "@/lib/api/logbook"
import { CHANNELS } from "@/lib/channels"
import { plural } from "@/lib/copy"

// Acronyms and fixed spellings in role and filter names: "ai engineer" -> "AI Engineer". "IT" stays out:
// sentence case would turn every "it" into "IT".
const ACRONYMS = [
  "AI",
  "ML",
  "LLM",
  "NLP",
  "API",
  "UI",
  "UX",
  "QA",
  "SRE",
  "HR",
  "GTM",
  "SDR",
  "BDR",
  "VP",
  "SVP",
  "EVP",
  "CEO",
  "CTO",
  "CFO",
  "COO",
  "CMO",
  "CPO",
  "CRO",
  "CISO",
  "SaaS",
  "iOS",
]

// Words that keep their own casing, keyed lowercase: the channel titles and the acronyms.
const CASED: Readonly<Record<string, string>> = {
  ...Object.fromEntries(Object.entries(CHANNELS).map(([channel, { title }]) => [channel, title])),
  ...Object.fromEntries(ACRONYMS.map((word) => [word.toLowerCase(), word])),
}

export type TextKind = "reason" | "worth" | "source" | "direction" | "cadence" | "labels"

// Plain words for the machine values; anything unmapped falls back to sentence case.
export const TEXT: Readonly<Record<TextKind, Readonly<Record<string, string>>>> = {
  reason: {
    worth_yes: "Keep",
    worth_no: "Skip",
    unjudged: "Not assessed",
    owner: "You (the owner)",
    human_worth: "Your call",
    family: "Family",
    romantic_partner: "Partner",
    partner: "Partner",
    // Reasons from before Luna's share call, until signals are refreshed.
    sensitive_context: "Sensitive context",
    sensitive_provider: "Clinician, lawyer, or banker",
    automated_sender: "Automated sender",
    stranger: "Stranger",
  },
  worth: { yes: "Keep", no: "Skip", unjudged: "Not assessed" },
  source: { human: "You", machine: "AI" },
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

export function humanize(value: string | number | null | undefined): string {
  return String(value ?? "")
    .replace(/^is_/, "")
    .replaceAll("_", " ")
    .trim()
}

/** Title case, brand names and acronyms kept: "vice-president" -> "Vice-President", "vp finance" -> "VP Finance". */
export function titleCase(value: string | number | null | undefined): string {
  const cap = (word: string) => CASED[word.toLowerCase()] ?? word.charAt(0).toUpperCase() + word.slice(1)
  return humanize(value)
    .split(" ")
    .filter(Boolean)
    .map((word) => word.split("-").map(cap).join("-"))
    .join(" ")
}

/** Sentence case, brand names and acronyms kept: "close_friend" -> "Close friend", "imessage" -> "iMessage". */
export function sentence(value: string | number | null | undefined): string {
  const words = humanize(value).split(" ").filter(Boolean)
  const [first] = words
  if (first === undefined) return ""
  const cased = words.map((word) => CASED[word.toLowerCase()] ?? word)
  if (!CASED[first.toLowerCase()]) cased[0] = first.charAt(0).toUpperCase() + first.slice(1)
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

/** The Sets page (pages/sets), and a member's relay presence for the Team page. Heartbeats come every 30 s; three missed and the member reads as last seen. */
const CONNECTED_SECONDS = 90
const SETS_CONNECTED = "Connected"

export const SETS = {
  title: "Sets",
  lead: "Manage your network sets and invite collaborators",
  newSet: "New Set",
  namePlaceholder: "Set name",
  create: "Create",
  cancel: "Cancel",
  delete: "Delete set",
  keep: "Keep",
  confirmDelete: (name: string) => `Delete “${name}” for everyone in it?`,
  uniqueContacts: (count: number) => `${count.toLocaleString()} unique contacts`,
  userHead: "User",
  contactsHead: "Contacts",
  joinedHead: "Joined",
  choose: "Choose a set",
  noneTitle: "No sets yet",
  loading: "Loading sets…",
  localIndex: "Local index",
  thisMac: "This Mac",
  localNote: "Your own network, searched on this Mac.",
  roleHead: "Role",
  invite: "Invite",
  inviteLink: "Send them this link:",
  members: (count: number) => plural(count, "member"),
  neverSeen: "Not seen yet",
  seen: (value: string, now: number = Date.now()) => {
    const seconds = Math.max(0, Math.floor((now - new Date(value).getTime()) / 1000))
    if (seconds < CONNECTED_SECONDS) return SETS_CONNECTED
    if (seconds < 3600) return `Seen ${Math.max(1, Math.floor(seconds / 60))}m ago`
    if (seconds < 86_400) return `Seen ${Math.floor(seconds / 3600)}h ago`
    return `Seen ${Math.floor(seconds / 86_400)}d ago`
  },
} as const

// The Build and View logbook actions on the bar and in the drawer, the reader, and the toasts.
export const LOGBOOK = {
  build: "Build logbook",
  building: "Building logbook…",
  view: "View logbook",
  explainer: "Raw Gmail, iMessage and WhatsApp messages, including groups, saved on this computer.",
  title: "Logbook",
  back: "People",
  all: "All logbooks",
  none: "No saved logbooks yet. Build one from People.",
  gone: "These logbooks aren't saved on this computer.",
  empty: "No messages in this conversation.",
  refresh: "Refresh logbook",
  refreshing: "Refreshing logbook…",
  filter: "Filter conversations",
  oldest: "Oldest",
  newest: "Newest",
  jump: "Jump to month",
  senders: "Senders",
  everyone: "Show everyone",
  fewer: "Show fewer",
  unread: "Couldn't read saved logbooks.",
  retry: "Retry",
} as const

/** Why a channel added nothing: its store is not on this computer, or could not be read. */
function channelGap({ channel, status }: ChannelCoverage): string | null {
  const title = sentence(channel)
  if (status === "missing") return `${title} isn't set up on this computer.`
  if (status === "unreadable") {
    return `${title} couldn't be read.`
  }
  return null
}

/** "Building a logbook for 3 people. Raw Gmail, … saved on this computer." */
export function logbookStartedToast(count: number): string {
  return `Building a logbook for ${plural(count, "person")}. ${LOGBOOK.explainer}`
}

/** "Saved 1,204 messages from 38 conversations." then each channel it could not read. */
export function logbookDoneToast(result: LogbookResult): string {
  const saved = result.messages
    ? `Saved ${plural(result.messages, "message")} from ${plural(result.files, "conversation")}.`
    : "No messages found on this computer."
  const gaps = result.channels.map(channelGap).filter((gap) => gap !== null)
  return [saved, ...gaps].join(" ")
}
