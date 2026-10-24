import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react"

import { CHANNEL_ICON, PowersetMark, Spinner } from "@/components/shared"
import { ConnectionRow as Row, type ConnectionState as State } from "@/components/shared/ConnectionRow"
import { Button } from "@/components/ui/button"
import { useCodexAccount } from "@/lib/agent/useCodexAccount"
import { fetchAccount } from "@/lib/api/account"
import { fetchSignedIn } from "@/lib/api/accounts"
import { signIn } from "@/lib/api/feedback"
import { errorText } from "@/lib/api/http"
import {
  connectSource,
  continueSetup,
  fetchConnection,
  installAction,
  type ConnectSource,
} from "@/lib/api/install"
import type { AccountSource } from "@/types/accounts"
import { CodexDetails } from "@/pages/settings/CodexDetails"
import { cn } from "@/lib/utils"
import { live, POLL_MS, prepare, standing } from "./sources"

const {
  gmail: GmailIcon,
  imessage: MessagesIcon,
  linkedin: LinkedInIcon,
  whatsapp: WhatsAppIcon,
} = CHANNEL_ICON

const SIGNED_IN = ["signed-in"]

/** The one line a row adds: what to do now, or the command a stopped sign-in needs. */
function Line({ children, bad = false }: { children: ReactNode; bad?: boolean }) {
  return <span className={cn("text-xs leading-snug", bad ? "text-bad" : "text-foreground")}>{children}</span>
}

// What to do while each sign-in runs. A row shows this, never the run's own status lines.
const TODO: Record<ConnectSource, string> = {
  linkedin: "Sign in to LinkedIn in the window that opens.",
  gmail: "Sign in to Google in the window that opens.",
  whatsapp: "In WhatsApp on your phone, go to Linked devices and scan this code.",
}

/** WhatsApp's code, in a box of its own size from the start, so nothing moves when it arrives. */
function CodeBox({ url }: { url?: string }) {
  return (
    <span className="mt-1 grid size-40 place-items-center rounded-[var(--radius-s)] bg-white p-2 text-muted-foreground">
      {url ? <img src={url} alt="WhatsApp code to scan" className="size-full" /> : <Spinner />}
    </span>
  )
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
 *  Full Disk Access comes before the browser sign-ins: turning it on makes macOS restart the app.
 *  After an import (`onDone`) there is no setup to run: the data is in, so Done, and no Back. */
export function Connect({
  onBack,
  onDone,
  details,
}: {
  onBack?: () => void
  onDone?: () => void
  details?: Record<AccountSource, ReactNode>
}) {
  const client = useQueryClient()
  const codex = useCodexAccount()
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [mailbox, setMailbox] = useState<string | null>(null)
  // Your LinkedIn profile, when setup goes without LinkedIn's sign-in.
  const [profile, setProfile] = useState<string | null>(null)
  // The mailboxes Gmail's last connect asked for, so Try again asks for them again.
  const [asked, setAsked] = useState<string[] | null>(null)
  // The sign-in a click started, from the click until its outcome is known: its row shows Working
  // throughout, never the last run's record or a sign-in answer from before it. `launched`: its
  // run has written to the connect record.
  const [queued, setQueued] = useState<{ source: ConnectSource; launched: boolean } | null>(null)
  // Powerset's sign-in in the browser, until the account is read again.
  const [signingIn, setSigningIn] = useState(false)
  // Full Disk Access is turned on in System Settings: Working until the window comes back.
  const [asking, setAsking] = useState(false)

  const account = useQuery({ queryKey: ["account"], queryFn: ({ signal }) => fetchAccount(signal) })
  const signedIn = account.data?.signed_in === true
  const powerset: State = account.isPending
    ? "checking"
    : signedIn
      ? "ready"
      : signingIn
        ? "working"
        : "needed"
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
    const again = () => void client.invalidateQueries({ queryKey: SIGNED_IN }).finally(() => setAsking(false))
    window.addEventListener("focus", again)
    return () => window.removeEventListener("focus", again)
  }, [client])
  const running = record !== undefined && live(record)
  // One sign-in at a time: setup's own lock holds the others off.
  const busy = queued !== null || running
  // Once a run is over (the one a click launched, or one under way when the screen opened), ask what
  // is signed in, then let its row show the answer.
  const wasRunning = useRef(false)
  useEffect(() => {
    const ended = wasRunning.current && !running
    wasRunning.current = running
    if (ended || (queued?.launched && !running)) {
      void client.invalidateQueries({ queryKey: ["accounts"] })
      void client.refetchQueries({ queryKey: SIGNED_IN }).finally(() => setQueued(null))
    }
  }, [queued, running, client])
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
    setQueued({ source, launched: false })
    try {
      await prepare(source)
      // Answers once the run has written to the connect record, so the record read next is its own.
      await connectSource(source, emails)
      await client.refetchQueries({ queryKey: ["install", "connect"] })
      setQueued({ source, launched: true })
    } catch (caught) {
      fail(caught)
      setQueued(null)
    }
  }

  const source = (
    name: ConnectSource,
    title: string,
    icon: ReactNode,
    why: ReactNode,
    // A line of the row's own, while it is not working.
    extra?: ReactNode,
  ) => {
    const run = standing(record, ready, name)
    const at = queued?.source === name && run.state !== "working" ? { state: "working" as const } : run
    const command = "command" in at ? at.command : undefined
    return (
      <Row
        key={name}
        icon={icon}
        title={title}
        details={details?.[name]}
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
        {at.state === "working" ? <Line>{TODO[name]}</Line> : null}
        {at.state === "working" && name === "whatsapp" ? (
          <CodeBox url={"qr" in at ? at.qr : undefined} />
        ) : null}
        {command ? (
          <>
            <Line bad>{"line" in at ? at.line : null}</Line>
            <code className="select-all break-all rounded-[var(--radius-s)] bg-surface-2 px-2 py-1 text-[11px]">
              {command}
            </code>
          </>
        ) : null}
        {at.state === "working" ? null : extra}
      </Row>
    )
  }

  return (
    <div className="flex w-full flex-col gap-4">
      <ul className="m-0 flex list-none flex-col divide-y divide-line overflow-hidden rounded-[var(--radius-m)] border border-line-strong bg-card p-0 text-left">
        <Row
          icon={<PowersetMark className="size-[18px]" />}
          title="Powerset"
          why={signedIn ? (account.data?.email ?? "") : "Your account, needed for setup."}
          state={powerset}
          action={{
            label: "Connect",
            onClick: () => {
              setError(null)
              setSigningIn(true)
              void signIn()
                .catch(fail)
                .finally(
                  () =>
                    void client.refetchQueries({ queryKey: ["account"] }).finally(() => setSigningIn(false)),
                )
            },
          }}
        >
          {powerset === "working" ? <Line>Finish signing in to Powerset in your browser.</Line> : null}
        </Row>
        <Row
          icon={<span className="font-mono text-[12px] font-bold">&gt;_</span>}
          title="Codex"
          details={details ? <CodexDetails codex={codex} /> : undefined}
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
          details={details?.imessage}
          why="iMessage and Contacts, with Full Disk Access."
          state={ready === undefined ? "checking" : ready.imessage ? "ready" : asking ? "working" : "needed"}
          action={{
            label: "Connect",
            onClick: () => {
              setAsking(true)
              void installAction("permissions").catch((caught: unknown) => {
                fail(caught)
                setAsking(false)
              })
            },
          }}
        >
          {asking && ready?.imessage === false ? (
            <Line>Turn on Powerpacks in System Settings, then come back here.</Line>
          ) : null}
        </Row>
        {source(
          "linkedin",
          "LinkedIn",
          <LinkedInIcon className="size-[18px]" />,
          "Your LinkedIn connections.",
          !linkedin && onBack && (processed || profile !== null) ? (
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
      {onDone ? (
        <div className="flex justify-end">
          <Button variant="primary" disabled={busy} onClick={onDone}>
            Done
          </Button>
        </div>
      ) : onBack ? (
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
      ) : null}
      {error && (
        <p role="alert" className="m-0 text-xs text-bad">
          {error}
        </p>
      )}
    </div>
  )
}
