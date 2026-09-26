// The page's words for machine values, ported 1:1 from the legacy people.js.

import type { Channel } from "@/types/people";

export const CHANNEL_TITLE: Readonly<Record<Channel, string>> = {
  gmail: "Gmail", imessage: "iMessage", whatsapp: "WhatsApp", linkedin: "LinkedIn",
};

// Brand casing, keyed lowercase: the channels plus JEV.
const BRANDS: Readonly<Record<string, string>> = { ...CHANNEL_TITLE, jev: "JEV" };

export type TextKind =
  | "share" | "reason" | "worth" | "source" | "mode" | "hierarchy" | "intro_source"
  | "seniority" | "function" | "direction" | "cadence" | "labels";

// Plain words for the machine values; anything unmapped falls back to sentence case.
export const TEXT: Readonly<Record<TextKind, Readonly<Record<string, string>>>> = {
  share: { yes: "Sharing", confirm: "Needs confirmation", no: "Not sharing" },
  reason: {
    worth_yes: "Worth: yes", worth_maybe: "Worth: maybe", worth_no: "Worth: no", owner: "You (the owner)",
    human_share: "You chose to share", human_private: "You chose to keep private",
    family: "Family", romantic_partner: "Partner", minor: "Minor", sensitive_context: "Sensitive context",
    sensitive_provider: "Clinician, lawyer, or banker", automated_sender: "Automated sender", stranger: "Stranger",
  },
  worth: { yes: "Yes", maybe: "Maybe", no: "No", unjudged: "Not assessed" },
  source: { human: "You", machine: "AI" },
  mode: { professional_only: "Work only", personal_only: "Personal only", mixed: "Work and personal" },
  hierarchy: { manager: "They managed you", peer: "Same level", report: "You managed them", none: "No reporting line" },
  intro_source: { mutual_friend: "Mutual friend", cold_outreach: "Unsolicited contact" },
  seniority: { mid: "Mid-level" },
  function: { founder_exec: "Founder or executive", people: "People and recruiting" },
  direction: { they_initiate: "Mostly them", i_initiate: "Mostly you", mutual: "Both" },
  cadence: { dormant: "No contact in over 2 years", stale: "No contact in over 1 year" },
  labels: {
    is_coworker_current: "Current colleague", is_coworker_past: "Former colleague",
    is_vendor_or_partner: "Vendor or partner", is_mentor_or_advisor: "Mentor or adviser",
    is_mentee_or_report: "Someone you mentor or manage", is_neighbor_or_local: "Neighbor or local contact",
    is_healthcare_legal_or_financial_provider: "Healthcare, legal, or financial provider",
    owner_would_intro: "You would introduce them", they_would_take_owner_call: "They would take your call",
    notable: "Publicly notable",
  },
};

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function humanize(value: string | null | undefined): string {
  return String(value ?? "").replace(/^is_/, "").replaceAll("_", " ").trim();
}

/** Sentence case, brand names kept: "close friend" -> "Close friend", "imessage" -> "iMessage". */
export function sentence(value: string | null | undefined): string {
  const words = humanize(value).split(" ").filter(Boolean);
  const [first] = words;
  if (first === undefined) return "";
  const cased = words.map((word) => BRANDS[word.toLowerCase()] ?? word);
  if (!BRANDS[first.toLowerCase()]) cased[0] = first.charAt(0).toUpperCase() + first.slice(1);
  return cased.join(" ");
}

export function label(kind: TextKind, value: string): string {
  return TEXT[kind][value] || sentence(value);
}

export function plural(count: number, noun: string): string {
  const word = count === 1 ? noun : noun === "person" ? "people" : `${noun}s`;
  return `${count.toLocaleString()} ${word}`;
}

/** ISO prefixes in a fact date to words: "2012-02 to 2012-03" -> "Feb 2012 to Mar 2012". */
export function eventDate(value: string | null | undefined): string {
  return String(value ?? "").replace(
    /\b(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?\b/g,
    (_match: string, year: string, month?: string, day?: string) => {
      const name = month ? (MONTHS[Number(month) - 1] ?? "") : "";
      if (day) return `${name} ${Number(day)}, ${year}`;
      return name ? `${name} ${year}` : year;
    },
  );
}

export function formatDate(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 10);
  return date.toLocaleDateString(undefined, { month: "short", year: "numeric" });
}
