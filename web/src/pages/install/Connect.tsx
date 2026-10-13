import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react"

import { CHANNEL_ICON, CheckIcon, PowersetMark, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { useCodexAccount } from "@/lib/agent/useCodexAccount"
import { fetchAccount } from "@/lib/api/account"
import { fetchSignedIn, type SignedIn } from "@/lib/api/accounts"
import { signIn } from "@/lib/api/feedback"
import { errorText } from "@/lib/api/http"
import {
  connectSource,
  continueSetup,
  fetchConnection,
  fetchPreflight,
  installAction,
  installPreflight,
  type ConnectSource,
  type Preflight,
} from "@/lib/api/install"
import { cn } from "@/lib/utils"
import type { InstallStatus, InstallStep } from "@/types/install"

const {
  gmail: GmailIcon,
  imessage: MessagesIcon,
  linkedin: LinkedInIcon,
  whatsapp: WhatsAppIcon,
} = CHANNEL_ICON

// While a sign-in or an install is still running, ask again this often.
const POLL_MS = 2_000
const SIGNED_IN = ["signed-in"]

// Each source's steps in the Connect screen's run, the last its login (status_prose.py).
const STEPS: Record<ConnectSource, readonly InstallStep[]> = {
  linkedin: ["linkedin_login"],
  gmail: ["gmail_tools", "gmail_login"],
  whatsapp: ["whatsapp_tools", "whatsapp_login"],
}

// What a source's sign-in needs on this Mac first (preflight.py): a browser for LinkedIn's and
// Google's sign-in pages, and Google's command-line tools for the Gmail app.
type Tool = "chromium" | "gcloud"
const NEEDS: Record<ConnectSource, readonly Tool[]> = {
  linkedin: ["chromium"],
  gmail: ["chromium", "gcloud"],
  whatsapp: [],
}
const TOOL = { chromium: "browser", gcloud: "gcloud" } as const

type State = "checking" | "working" | "ready" | "needed"

// Every row's right side: one slot of one width, an icon beside a word.
const SLOT = "w-28 shrink-0 self-center"

/** One thing to connect: what it is for, then its slot: Connect while it is not connected,
 *  "Working" while its sign-in runs, a check once connected. */
function Row({
  icon,
  title,
  why,
  state,
  action,
  children,
}: {
  icon: ReactNode
  title: string
  why: ReactNode
  state: State
  action?: { label: string; onClick: () => void; disabled?: boolean }
  // What the step says while it works or stopped: its line, a code to scan, a command.
  children?: ReactNode
}) {
  return (
    <li className="flex items-start gap-3 px-4 py-3.5">
      <span className="grid size-9 shrink-0 place-items-center rounded-[var(--radius-s)] bg-surface-2 text-foreground">
        {icon}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-[13.5px] font-bold">{title}</span>
        <span className="text-xs leading-snug text-muted-foreground">{why}</span>
        {children}
      </div>
      {state === "needed" && action ? (
        <Button
          size="sm"
          onClick={action.onClick}
          disabled={action.disabled}
          aria-label={`${title}: ${action.label}`}
          className={SLOT}
        >
          {action.label}
        </Button>
      ) : (
        <span
          className={cn(
            SLOT,
            "rise-in inline-flex items-center justify-center gap-1.5 text-xs font-semibold",
            state === "ready" ? "text-ok" : "text-muted-foreground",
          )}
        >
          {state === "ready" ? <CheckIcon className="size-3.5" /> : <Spinner />}
          {state === "ready" ? "Connected" : state === "working" ? "Working" : "Checking"}
        </span>
      )}
    </li>
  )
}

/** A line under a row's purpose: what its step is doing, or why it stopped. */
function Line({ children, bad = false }: { children: ReactNode; bad?: boolean }) {
  return <span className={cn("text-xs leading-snug", bad ? "text-bad" : "text-foreground")}>{children}</span>
}

/** Where one source stands: its sign-in in progress (the connect record), else whether it is
 *  signed in (accounts.signed_in, the answer setup takes). Only signing in makes a source ready. */
function standing(record: InstallStatus | undefined, signed: SignedIn | undefined, source: ConnectSource) {
  if (record && STEPS[source].includes(record.step)) {
    const live = (record.installer_pid ?? 0) > 0
    if (record.status === "running" || (record.status === "waiting" && live))
      return { state: "working" as const, line: record.message, qr: record.action?.qr_url }
    // Stopped: failed, or waiting with nothing left running (a password, a closed window).
    if (record.status === "failed" || record.status === "waiting")
      return {
        state: "needed" as const,
        line: record.message,
        command: record.action?.command,
        stopped: true,
      }
  }
  if (signed === undefined) return { state: "checking" as const }
  return { state: signed[source] ? ("ready" as const) : ("needed" as const) }
}

const LINK =
  "cursor-pointer border-0 bg-transparent p-0 text-xs text-primary hover:underline disabled:opacity-50"

/** A value typed in place, saved on Enter or leaving the field; one that is not `valid` is dropped. */
function Field({
  initial,
  label,
  placeholder,
  type,
  valid,
  onDone,
}: {
  initial: string
  label: string
  placeholder: string
  type: "email" | "url"
  valid: (value: string) => boolean
  onDone: (value: string | null) => void
}) {
  const [value, setValue] = useState(initial)
  const save = (event: FormEvent) => {
    event.preventDefault()
    onDone(valid(value.trim()) ? value.trim() : null)
  }
  return (
    <form onSubmit={save} className="flex gap-2">
      <input
        type={type}
        aria-label={label}
        ref={(input) => input?.focus()}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onBlur={save}
        placeholder={placeholder}
        className="h-7 min-w-0 flex-1 rounded-[var(--radius-s)] border border-line-strong bg-card px-2 text-xs text-foreground outline-none focus-visible:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))]"
      />
    </form>
  )
}

/** Who you are on LinkedIn, when setup goes without its sign-in: deep context starts from your own
 *  profile (deep_context_v2/owner.py), which the sign-in otherwise reads. */
function ProfileLink({ url, onChange }: { url: string | null; onChange: (url: string) => void }) {
  const [typing, setTyping] = useState(false)
  if (typing)
    return (
      <Field
        initial={url ?? ""}
        label="LinkedIn profile link"
        placeholder="linkedin.com/in/you"
        type="url"
        valid={(value) => value.includes("linkedin.com/in/")}
        onDone={(value) => {
          setTyping(false)
          if (value) onChange(value)
        }}
      />
    )
  return (
    <span className="text-xs text-muted-foreground">
      {url ? `Your profile: ${url.replace(/^(https?:\/\/)?(www\.)?/, "")} · ` : "Not signing in? "}
      <button type="button" className={LINK} onClick={() => setTyping(true)}>
        {url ? "Change" : "Add your profile link"}
      </button>
    </span>
  )
}

/** Gmail's mailboxes: the ones its app already takes, each shown with Add another; before there
 *  is an app, the address the first connect uses (the Powerset one until changed). The first
 *  mailbox owns the app, so it stays first. */
function Mailboxes({
  known,
  email,
  disabled,
  onChange,
  onAdd,
}: {
  known: readonly string[]
  email: string
  disabled: boolean
  onChange: (email: string) => void
  onAdd: (email: string) => void
}) {
  const [typing, setTyping] = useState(false)
  if (typing)
    return (
      <Field
        initial={known.length ? "" : email}
        label="Gmail address"
        placeholder="you@gmail.com"
        type="email"
        valid={(value) => value.includes("@")}
        onDone={(value) => {
          setTyping(false)
          if (value) (known.length ? onAdd : onChange)(value)
        }}
      />
    )
  if (known.length)
    return (
      <span className="flex flex-wrap items-center gap-1.5">
        {known.map((mailbox) => (
          <span key={mailbox} className="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] text-foreground">
            {mailbox}
          </span>
        ))}
        <button type="button" className={LINK} disabled={disabled} onClick={() => setTyping(true)}>
          Add another
        </button>
      </span>
    )
  return (
    <>
      Your mail, from {email || "your Google account"}
      {" · "}
      <button type="button" className={LINK} onClick={() => setTyping(true)}>
        Change
      </button>
    </>
  )
}

/** The desktop app's setup before it starts: every sign-in, one row each, so setup itself never
 *  waits on the user. Only Powerset is needed; a source left unconnected is left out of setup.
 *  Full Disk Access comes before the browser sign-ins: turning it on makes macOS restart the app. */
export function Connect({ onBack }: { onBack: () => void }) {
  const client = useQueryClient()
  const codex = useCodexAccount()
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [mailbox, setMailbox] = useState<string | null>(null)
  // Your LinkedIn profile, when setup goes without LinkedIn's sign-in.
  const [profile, setProfile] = useState<string | null>(null)
  // The mailboxes Gmail's last connect asked for, so Try again asks for them again.
  const [asked, setAsked] = useState<string[] | null>(null)
  // A source waiting on its tools to install before its sign-in starts.
  const [queued, setQueued] = useState<ConnectSource | null>(null)

  const account = useQuery({ queryKey: ["account"], queryFn: ({ signal }) => fetchAccount(signal) })
  const signedIn = account.data?.signed_in === true
  const connection = useQuery({
    queryKey: ["install", "connect"],
    queryFn: ({ signal }) => fetchConnection(signal),
    refetchInterval: POLL_MS,
  })
  const record = connection.data
  // What is signed in, from where each sign-in lives; asked again on coming back to the window
  // (from System Settings or a browser) and once a sign-in here ends.
  const signed = useQuery({ queryKey: SIGNED_IN, queryFn: ({ signal }) => fetchSignedIn(signal) })
  const ready = signed.data
  useEffect(() => {
    const again = () => void client.invalidateQueries({ queryKey: SIGNED_IN })
    window.addEventListener("focus", again)
    return () => window.removeEventListener("focus", again)
  }, [client])
  // One sign-in at a time: setup's own lock holds the others off.
  const busy =
    queued !== null ||
    (record !== undefined &&
      (record.status === "running" || (record.status === "waiting" && (record.installer_pid ?? 0) > 0)))
  const wasBusy = useRef(false)
  useEffect(() => {
    if (wasBusy.current && !busy) void client.invalidateQueries({ queryKey: SIGNED_IN })
    wasBusy.current = busy
  }, [busy, client])
  const email = mailbox ?? account.data?.email ?? ""
  const linkedin = ready?.linkedin === true
  // The Gmail mailboxes msgvault keeps, signed in or not, the app's owner first: a connect sends
  // them all, so the owner stays first. Before there is one, the first connect starts from `email`.
  const known = ready?.mailboxes ?? []
  // Deep context starts from your LinkedIn profile: needed once any source will be processed and
  // LinkedIn's sign-in (which reads it) is not used. With nothing signed in setup processes nothing.
  const processed = ready !== undefined && (ready.imessage || ready.gmail || ready.whatsapp)
  const needsProfile = !linkedin && profile === null && processed
  const connectGmail = (emails: string[]) => {
    setAsked(emails)
    void connect("gmail", emails)
  }

  const fail = (caught: unknown) => setError(errorText(caught))
  // Install what the sign-in needs (a browser, Google's tools), wait for it, then start it.
  const connect = async (source: ConnectSource, emails?: string[]) => {
    setError(null)
    setQueued(source)
    try {
      const needed = (checks: Preflight) => NEEDS[source].map((tool) => checks[TOOL[tool]])
      const checks = await fetchPreflight()
      for (const tool of NEEDS[source]) if (!checks[TOOL[tool]].ok) await installPreflight(tool)
      for (let now = checks; !needed(now).every((check) => check.ok); now = await fetchPreflight()) {
        const broken = needed(now).find((check) => check.install?.status === "failed")
        if (broken) throw new Error(broken.install?.message ?? "Could not install what this sign-in needs")
        await new Promise((resolve) => setTimeout(resolve, POLL_MS))
      }
      await connectSource(source, emails)
      await client.invalidateQueries({ queryKey: ["install", "connect"] })
    } catch (caught) {
      fail(caught)
    } finally {
      setQueued(null)
    }
  }

  const source = (
    name: ConnectSource,
    title: string,
    icon: ReactNode,
    why: ReactNode,
    // A line of the row's own under its run's.
    extra?: ReactNode,
  ) => {
    const at =
      queued === name
        ? { state: "working" as const, line: "Installing what it needs…" }
        : standing(record, ready, name)
    const qr = "qr" in at ? at.qr : undefined
    const command = "command" in at ? at.command : undefined
    return (
      <Row
        key={name}
        icon={icon}
        title={title}
        why={why}
        state={at.state}
        action={{
          label: "stopped" in at ? "Try again" : "Connect",
          onClick: () =>
            name === "gmail"
              ? connectGmail(asked ?? (known.length ? [...known] : [email]))
              : void connect(name),
          disabled: busy || (name === "gmail" && !email.includes("@")),
        }}
      >
        {"line" in at && at.line && !qr ? <Line bad={"stopped" in at}>{at.line}</Line> : null}
        {command ? (
          <code className="select-all break-all rounded-[var(--radius-s)] bg-surface-2 px-2 py-1 text-[11px]">
            {command}
          </code>
        ) : null}
        {qr ? (
          <img
            src={qr}
            alt="WhatsApp code to scan"
            className="mt-1 size-40 rounded-[var(--radius-s)] bg-white p-2"
          />
        ) : null}
        {extra}
      </Row>
    )
  }

  return (
    <div className="flex w-full flex-col gap-4">
      <ul className="m-0 flex list-none flex-col divide-y divide-line rounded-[var(--radius-m)] border border-line-strong bg-card p-0 text-left">
        <Row
          icon={<PowersetMark className="size-[18px]" />}
          title="Powerset"
          why={signedIn ? (account.data?.email ?? "") : "Your account, needed for setup."}
          state={account.isPending ? "checking" : signedIn ? "ready" : "needed"}
          action={{
            label: "Connect",
            onClick: () => {
              setError(null)
              void signIn()
                .catch(fail)
                .finally(() => void client.invalidateQueries({ queryKey: ["account"] }))
            },
          }}
        />
        <Row
          icon={<span className="font-mono text-[12px] font-bold">&gt;_</span>}
          title="Codex"
          why="Chat, on your ChatGPT plan."
          state={
            codex.loading
              ? "checking"
              : codex.status?.installed && codex.status.account
                ? "ready"
                : codex.login
                  ? "working"
                  : "needed"
          }
          action={{ label: "Connect", onClick: codex.connect, disabled: !codex.status?.installed }}
        >
          {codex.login ? <Line>Finish signing in to ChatGPT in your browser.</Line> : null}
        </Row>
        <Row
          icon={<MessagesIcon className="size-[18px]" />}
          title="Messages"
          why="iMessage and Contacts, with Full Disk Access."
          state={ready === undefined ? "checking" : ready.imessage ? "ready" : "needed"}
          action={{ label: "Connect", onClick: () => void installAction("permissions").catch(fail) }}
        />
        {source(
          "linkedin",
          "LinkedIn",
          <LinkedInIcon className="size-[18px]" />,
          "Your LinkedIn connections.",
          !linkedin && (processed || profile !== null) ? (
            <ProfileLink url={profile} onChange={setProfile} />
          ) : null,
        )}
        {source(
          "gmail",
          "Gmail",
          <GmailIcon className="size-[18px]" />,
          <Mailboxes
            known={known}
            email={email}
            disabled={busy}
            onChange={setMailbox}
            onAdd={(added) => connectGmail([...known, added])}
          />,
        )}
        {source(
          "whatsapp",
          "WhatsApp",
          <WhatsAppIcon className="size-[18px]" />,
          "Your WhatsApp chats, by scanning a code.",
        )}
      </ul>
      <div className="flex items-center justify-between gap-3">
        <Button disabled={starting} onClick={onBack}>
          Back
        </Button>
        <span className="text-xs text-muted-foreground">
          {signedIn && needsProfile
            ? "Sign in to LinkedIn or add your profile link."
            : "Anything left unconnected is skipped."}
        </span>
        <Button
          variant="primary"
          disabled={!signedIn || ready === undefined || needsProfile || busy || starting}
          onClick={() => {
            setStarting(true)
            setError(null)
            // Without LinkedIn's sign-in, setup's owner step reads the profile from its link.
            void continueSetup(linkedin || profile === null ? {} : { linkedinUrl: profile }).catch(
              (caught: unknown) => {
                fail(caught)
                setStarting(false)
              },
            )
          }}
        >
          {starting ? "Starting setup…" : "Set up"}
        </Button>
      </div>
      {error && (
        <p role="alert" className="m-0 text-xs text-bad">
          {error}
        </p>
      )}
    </div>
  )
}
