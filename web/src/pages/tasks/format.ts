// The Refresh page's words for times, counts and runs.

import type { RefreshRun, RefreshSource } from "@/types/refresh"

export const SOURCE_TITLE: Readonly<Record<RefreshSource, string>> = {
  linkedin: "LinkedIn",
  gmail: "Gmail",
  imessage: "iMessage",
  whatsapp: "WhatsApp",
}

/** "6:00 AM" from "06:00". */
export function clock(time: string): string {
  const [hour = 0, minute = 0] = time.split(":").map(Number)
  return `${hour % 12 || 12}:${String(minute).padStart(2, "0")} ${hour < 12 ? "AM" : "PM"}`
}

/** "Today 6:00 AM", "Yesterday 9:42 PM", "Tomorrow 6:00 AM", else "Mon, Nov 3, 6:00 AM". */
export function when(value: string, now: Date = new Date()): string {
  const date = new Date(value)
  const time = date.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
  const days = Math.round((startOfDay(date) - startOfDay(now)) / 86_400_000)
  if (days === 0) return `Today ${time}`
  if (days === -1) return `Yesterday ${time}`
  if (days === 1) return `Tomorrow ${time}`
  return date.toLocaleString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  })
}

export function lower(text: string): string {
  return /^(Today|Yesterday|Tomorrow)/.test(text) ? text.charAt(0).toLowerCase() + text.slice(1) : text
}

export function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
}

export function stamp(value: string): string {
  return new Date(value).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  })
}

export function trigger(run: RefreshRun): string {
  if (run.trigger === "manual") return "Manual"
  return run.missed_days ? `Daily · caught up ${run.missed_days + 1} days` : "Daily"
}

export function added(run: RefreshRun): string {
  if (run.people_before === null || run.people_after === null) return "—"
  const delta = run.people_after - run.people_before
  return delta > 0 ? `+${delta.toLocaleString()}` : String(delta)
}

export function people(run: RefreshRun): string {
  if (run.people_after === null) return run.people_before === null ? "—" : run.people_before.toLocaleString()
  if (run.people_before === null) return run.people_after.toLocaleString()
  return `${run.people_before.toLocaleString()} → ${run.people_after.toLocaleString()} (${added(run)})`
}

export function took(run: RefreshRun): string {
  if (!run.finished_at) return "—"
  const minutes = Math.round(
    (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 60_000,
  )
  return minutes < 1 ? "<1 min" : `${minutes} min`
}

export function lastLine(run: RefreshRun): string {
  const delta = added(run)
  const peopleText = delta === "—" ? "" : ` · ${delta === "0" ? "no new people" : `${delta} people`}`
  return `Last run ${lower(when(run.started_at))}${peopleText}`
}
