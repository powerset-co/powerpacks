import { invoke } from "@/lib/desktop"
import { body, failure } from "@/lib/api/http"
import type { InstallStatus } from "@/types/install"

export async function fetchInstall(signal: AbortSignal): Promise<InstallStatus> {
  const response = await fetch("/api/install", { signal, cache: "no-store" })
  if (!response.ok) throw await failure(response, "Couldn't read installation progress")
  return body<InstallStatus>(response)
}

export async function installAction(action: string, values: object = {}): Promise<void> {
  const response = await fetch(`/api/install/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  })
  if (!response.ok) throw await failure(response, "Couldn't continue setup")
}

/** The sources the Connect screen signs in to before setup (controller.py CONNECTABLE). */
export type ConnectSource = "linkedin" | "gmail" | "whatsapp"

/** The Connect screen's sign-in in progress (one source's tools and login at a time, in its own
 *  record): what it is doing now, or why it stopped. Whether a source is signed in is fetchSignedIn's. */
export async function fetchConnection(signal: AbortSignal): Promise<InstallStatus> {
  const response = await fetch("/api/install/connect", { signal, cache: "no-store" })
  if (!response.ok) throw await failure(response, "Couldn't check your connections")
  return body<InstallStatus>(response)
}

/** Sign in to one source before setup; Gmail's takes every mailbox, the app's owner first.
 *  fetchConnection follows it. */
export function connectSource(source: ConnectSource, emails?: string[]): Promise<void> {
  return installAction("connect", { source, emails })
}

/** One person of the first look: from the index's people file (controller.first_look). */
export interface FirstLookPerson {
  public_identifier: string
  name: string
  title: string
  company: string
  headline: string
  avatar_url: string
}

/** One question the first look offers, minted from the index (controller.questions): what the
 *  index holds most of, so it always answers with real names. */
export interface FirstLookQuestion {
  key: string
  text: string
  count: number
}

export interface FirstLook {
  people: FirstLookPerson[]
  total: number
  questions: FirstLookQuestion[]
}

/** A question's answer from the index: the first people and how many there are. */
export interface FirstLookAnswer {
  question: string
  people: FirstLookPerson[]
  total: number
}

export async function fetchFirstLookAnswer(key: string, signal?: AbortSignal): Promise<FirstLookAnswer> {
  const response = await fetch(`/api/install/first-look/ask?${new URLSearchParams({ key }).toString()}`, {
    signal,
    cache: "no-store",
  })
  if (!response.ok) throw await failure(response, "Couldn't answer that")
  return body<FirstLookAnswer>(response)
}

/** The first people to show once the index is built, and how many there are in all. */
export async function fetchFirstLook(signal?: AbortSignal): Promise<FirstLook> {
  const response = await fetch("/api/install/first-look", { signal, cache: "no-store" })
  if (!response.ok) throw await failure(response, "Couldn't read your people")
  return body<FirstLook>(response)
}

/** Move the LinkedIn sign-in and the connections it read aside (each to a `.bkup-<time>`), so the
 *  onboarding's LinkedIn steps run again from the sign-in. For trying the onboarding over. */
export function forgetLinkedin(): Promise<void> {
  return installAction("linkedin-forget")
}

/** What the user answered to get setup past a wait (desktop/src-tauri/src/onboard.rs Answer). */
export interface SetupAnswer {
  linkedinUrl?: string
  /** The run's sources: LinkedIn alone from the LinkedIn step, every source from Sources. */
  sources?: string[]
}

/** Resumes setup in the desktop app, which runs it instead of an agent. */
export async function continueSetup(answer: SetupAnswer = {}): Promise<void> {
  await invoke("onboard_continue", { answer })
}

/** Bring the app forward after a sign-in that had to happen in the browser. */
export function focusApp(): Promise<void> {
  return invoke("app_focus").then(() => undefined)
}

/** Open a page in the system browser. */
export function openExternal(url: string): Promise<void> {
  return invoke("open_external", { url }).then(() => undefined)
}

/** Skip for now: setup counts as done on later launches (desktop/src-tauri onboard.rs set_skipped). */
export function setupSkip(): Promise<void> {
  return invoke("setup_skip").then(() => undefined)
}

/** Whether the app's setup is done: run here, skipped, or imported (desktop/src-tauri onboard.rs). */
export async function setupDone(): Promise<boolean> {
  return (await invoke("setup_done")) === true
}

/** The command-line install on this machine whose data the app can import, as a path to show. */
export async function importSource(): Promise<string | null> {
  const path = await invoke("setup_import_source")
  return typeof path === "string" ? path : null
}

/** Import that install's data: the app then uses its network, imports and account. */
export async function importData(): Promise<void> {
  await invoke("setup_import")
}

/** One preflight item: whether it is ready, and its background install once one was asked for. */
export interface PreflightInstall {
  status: "running" | "ok" | "failed"
  line?: string
  message?: string
}

/** What the desktop app's first screen checks (packs/powerset/primitives/install/preflight.py). */
export interface Preflight {
  browser: { ok: boolean; name: string | null; install: PreflightInstall | null }
  gcloud: { ok: boolean; install: PreflightInstall | null }
}

export async function fetchPreflight(signal?: AbortSignal): Promise<Preflight> {
  const response = await fetch("/api/install/preflight", { signal, cache: "no-store" })
  if (!response.ok) throw await failure(response, "Couldn't check this Mac")
  return body<Preflight>(response)
}

/** Install a preflight item in the background; fetchPreflight follows it. */
export function installPreflight(item: "chromium" | "gcloud"): Promise<void> {
  return installAction("preflight", { item })
}
