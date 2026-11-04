import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Fragment, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react"
import { Globe } from "lucide-react"
import PhoneInput, { isPossiblePhoneNumber } from "react-phone-number-input/input"

import { CHANNEL_ICON, PowersetMark, Spinner } from "@/components/shared"
import {
  ConnectionRow as Row,
  type ConnectionState as State,
  type ConnectionRowProps,
} from "@/components/shared/ConnectionRow"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { useCodexAccount } from "@/lib/agent/useCodexAccount"
import { fetchAccount } from "@/lib/api/account"
import { fetchChromeProfiles, fetchSignedIn } from "@/lib/api/accounts"
import { signIn } from "@/lib/api/feedback"
import { errorText } from "@/lib/api/http"
import { desktopPlatform, invoke, listen } from "@/lib/desktop"
import {
  connectSource,
  continueSetup,
  fetchConnection,
  installAction,
  linkWhatsappByPhone,
  stopWhatsappLink,
  type ConnectSource,
} from "@/lib/api/install"
import type { AccountSource } from "@/types/accounts"
import { BrowserProfiles } from "@/pages/settings/BrowserProfiles"
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
  whatsapp: "In WhatsApp on your phone, go to Linked devices and scan the code.",
}

// After this long on the QR without a link, the box says why a scan may not take and points to the
// phone number.
const SLOW_SCAN_MS = 60_000

/** The WhatsApp code, in one place: a large card that opens by itself as soon as WhatsApp waits on a
 *  scan, so a phone reads it before it changes (every 20 seconds). Closed, "Show code" reopens it.
 *  When the scan won't work, the person links by phone number instead (US unless it starts with +):
 *  WhatsApp sends a code to type on the phone, shown here the way the phone asks for it. Once
 *  WhatsApp refused a scan (`failed`, why) the box asks for the number by itself. Stop linking ends
 *  the link, so another sign-in can start. */
function CodeBox({ url, pairCode, failed }: { url?: string; pairCode?: string; failed?: string }) {
  const [open, setOpen] = useState(true)
  const [chosePhone, setByPhone] = useState(false)
  const byPhone = chosePhone || failed !== undefined
  const [slow, setSlow] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [phone, setPhone] = useState<string | undefined>()
  const [asked, setAsked] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const timer = setTimeout(() => setSlow(true), SLOW_SCAN_MS)
    return () => clearTimeout(timer)
  }, [])
  // A refused scan opens the box again, unless it was closed since that refusal came.
  const [dismissed, setDismissed] = useState<string | undefined>()
  const shown = open || (failed !== undefined && dismissed !== failed)
  const show = (next: boolean) => {
    setOpen(next)
    if (!next) setDismissed(failed)
  }
  const stop = () => {
    setStopping(true)
    show(false)
    stopWhatsappLink().catch((caught: unknown) => {
      setStopping(false)
      setError(errorText(caught))
    })
  }
  const ask = (event: FormEvent) => {
    event.preventDefault()
    if (!phone) return
    setError(null)
    setAsked(true)
    linkWhatsappByPhone(phone).catch((caught: unknown) => {
      setAsked(false)
      setError(errorText(caught))
    })
  }
  const [title, line] = !byPhone
    ? [
        "Scan with WhatsApp",
        "On your phone: Settings → Linked devices → Link a device, not the QR icon by your name (that one shares your contact). The code changes every 20 seconds; this one updates by itself.",
      ]
    : pairCode
      ? [
          "Enter this code on your phone",
          "In WhatsApp: Settings → Linked devices → Link a device → Link with phone number instead.",
        ]
      : [
          "Link with your phone number",
          failed
            ? `${failed} WhatsApp sends a code for this number; you type it on your phone.`
            : "WhatsApp sends a code for this number; you type it on your phone.",
        ]
  const stopButton = (
    <button type="button" className={LINK} disabled={stopping} onClick={stop}>
      {stopping ? "Stopping…" : "Stop linking"}
    </button>
  )
  return (
    <Dialog open={shown} onOpenChange={show}>
      <span className="flex items-center gap-3">
        <DialogTrigger className={LINK} disabled={stopping}>
          Show code
        </DialogTrigger>
        {stopButton}
      </span>
      {error && !shown ? <span className="text-xs text-bad">{error}</span> : null}
      <DialogContent className="w-[min(420px,100%)] items-center border-0 bg-[#f2f2f2] p-6 text-[#111]">
        <DialogHeader className="w-full">
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription className="text-[#555]">{line}</DialogDescription>
        </DialogHeader>
        {!byPhone ? (
          <>
            <span className="grid size-80 place-items-center rounded-[var(--radius-s)] bg-white p-3 text-[#555]">
              {url ? (
                <img
                  src={url}
                  alt="WhatsApp code to scan"
                  className="size-full [image-rendering:pixelated]"
                />
              ) : (
                <Spinner />
              )}
            </span>
            {slow ? (
              <span className="text-center text-xs text-[#555]">
                Not linking? Scan from Linked devices, or use your phone number instead.
              </span>
            ) : null}
            <Button onClick={() => setByPhone(true)}>Use your phone number instead</Button>
          </>
        ) : pairCode ? (
          <PairCode code={pairCode} />
        ) : asked ? (
          <span className="flex items-center gap-2 text-sm text-[#555]">
            <Spinner />
            Asking WhatsApp for a code…
          </span>
        ) : (
          <form className="flex w-full flex-col gap-2" onSubmit={ask}>
            <PhoneInput
              aria-label="WhatsApp phone number"
              defaultCountry="US"
              value={phone}
              onChange={setPhone}
              placeholder="(415) 555-0100"
              className="h-9 rounded-[var(--radius-s)] border border-[#ccc] bg-white px-3 text-sm text-[#111]"
            />
            <span className="text-xs text-[#555]">Outside the US? Start with + and your country code.</span>
            {error ? <span className="text-xs text-bad">{error}</span> : null}
            <Button type="submit" disabled={!phone || !isPossiblePhoneNumber(phone)}>
              Get code
            </Button>
          </form>
        )}
        {stopButton}
      </DialogContent>
    </Dialog>
  )
}

/** The pairing code as WhatsApp's phone screen asks for it: four boxes, a dash, four boxes. */
function PairCode({ code }: { code: string }) {
  const letters = code.replace(/[^0-9A-Za-z]/g, "").split("")
  return (
    <div role="group" aria-label={`Pairing code ${code}`} className="flex items-center gap-1">
      {letters.map((letter, index) => (
        <Fragment key={index}>
          {index === 4 ? <span className="px-0.5 text-[20px] text-[#555]">–</span> : null}
          <span className="grid h-11 w-9 place-items-center rounded-[6px] border border-[#ccc] bg-white font-mono text-[20px] font-semibold">
            {letter}
          </span>
        </Fragment>
      ))}
    </div>
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
      <button type="button" className={LINK} disabled={disabled} onClick={() => setTyping(true)}>
        Change
      </button>
    </>
  )
}

/** The desktop app's setup before it starts: every sign-in, one row each, so setup itself never
 *  waits on the user. Only Powerset is needed; a source left unconnected is left out of setup.
 *  Full Disk Access comes before the browser sign-ins: turning it on makes macOS restart the app.
 *  After an import (`onDone`) there is no setup to run: the data is in, so Done, and no Back; the
 *  onboarding's Sources starts setup from the same button, so it names it Start (`doneLabel`). */
// "browser" shows on Accounts alone, so it is not one of the rows Connect lists by itself.
export type ConnectRow = "powerset" | "codex" | "browser" | "imessage" | "linkedin" | "gmail" | "whatsapp"
const ALL_ROWS: readonly ConnectRow[] = ["powerset", "codex", "imessage", "linkedin", "gmail", "whatsapp"]

export function Connect({
  onBack,
  onDone,
  doneLabel = "Done",
  details,
  rows = ALL_ROWS,
  onGuide,
  layout,
  disabled = false,
}: {
  disabled?: boolean
  layout?: (items: Readonly<Record<ConnectRow, ConnectionRowProps>>) => ReactNode
  onBack?: () => void
  onDone?: () => void
  doneLabel?: string
  details?: Record<AccountSource, ReactNode>
  /** Which rows to show, in this order's subset; the onboarding's Sources shows the message sources alone. */
  rows?: readonly ConnectRow[]
  /** The onboarding's guide: told which source to show on its icon's click and on its Connect. */
  onGuide?: (source: "imessage" | "gmail" | "whatsapp") => void
}) {
  const shown = new Set(rows)
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
  const [asking, setAsking] = useState(false)

  const account = useQuery({ queryKey: ["account"], queryFn: ({ signal }) => fetchAccount(signal) })
  // Only Accounts shows the browser row.
  const chrome = useQuery({
    queryKey: ["chrome-profiles"],
    queryFn: ({ signal }) => fetchChromeProfiles(signal),
    enabled: details !== undefined,
  })
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
  const signed = useQuery({
    queryKey: SIGNED_IN,
    queryFn: ({ signal }) => fetchSignedIn(signal),
    refetchInterval: asking ? POLL_MS : false,
    refetchIntervalInBackground: true,
  })
  const ready = signed.data
  useEffect(() => {
    const again = () => void client.invalidateQueries({ queryKey: SIGNED_IN })
    window.addEventListener("focus", again)
    return () => window.removeEventListener("focus", again)
  }, [client])
  useEffect(() => {
    if (desktopPlatform() !== "mac") return
    return listen("permission-ended", () => {
      setAsking(false)
      void client.invalidateQueries({ queryKey: SIGNED_IN })
    })
  }, [client])
  useEffect(() => {
    if (!asking || ready?.imessage !== false) return
    let active = true
    const mac = desktopPlatform() === "mac"
    void installAction("permissions")
      .then(() => (active && mac ? invoke("permission_begin") : undefined))
      .catch((caught: unknown) => {
        if (active) setError(errorText(caught))
      })
    return () => {
      active = false
      if (mac) void invoke("permission_end").catch((caught: unknown) => setError(errorText(caught)))
    }
  }, [asking, ready?.imessage])
  const running = record !== undefined && live(record)
  // One sign-in at a time: setup's own lock holds the others off.
  const busy = disabled || queued !== null || running
  // While a sign-in, a sync or the Full Disk Access ask is under way, every other way to connect
  // waits: nothing on this screen starts a second one.
  const locked = busy || asking
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
    const guide = name === "linkedin" || !onGuide ? undefined : () => onGuide(name)
    return {
      icon,
      title,
      why,
      state: at.state,
      details: details?.[name],
      onIcon: guide,
      action: {
        label: "stopped" in at ? "Try again" : "Connect",
        onClick: () => {
          guide?.()
          if (name === "gmail") connectGmail(asked ?? (known.length ? [...known] : [email]))
          else void connect(name)
        },
        disabled: locked || (name === "gmail" && !email.includes("@")),
      },
      children: (
        <>
          {at.state === "working" ? <Line>{TODO[name]}</Line> : null}
          {at.state === "working" && name === "whatsapp" ? (
            <CodeBox
              url={"qr" in at ? at.qr : undefined}
              pairCode={"pairCode" in at ? at.pairCode : undefined}
              failed={"qrFailed" in at ? at.qrFailed : undefined}
            />
          ) : null}
          {command ? (
            <>
              <Line bad>{"line" in at ? at.line : null}</Line>
              <code className="select-all break-all rounded-[var(--radius-s)] bg-surface-2 px-2 py-1 text-[11px]">
                {command}
              </code>
            </>
          ) : name === "whatsapp" && "stopped" in at ? (
            // WhatsApp's stop and its reason (a refused scan, Stop linking), with no command to run.
            <>
              <Line bad>{at.line}</Line>
              {at.detail ? <Line>{at.detail}</Line> : null}
            </>
          ) : null}
          {at.state === "working" ? null : extra}
        </>
      ),
    } satisfies ConnectionRowProps
  }
  const items: Record<ConnectRow, ConnectionRowProps> = {
    powerset: {
      icon: <PowersetMark className="size-[18px]" />,
      title: "Powerset",
      why: signedIn ? (account.data?.email ?? "") : "Your account, needed for setup.",
      state: powerset,
      action: {
        label: "Connect",
        disabled: locked,
        onClick: () => {
          setError(null)
          setSigningIn(true)
          void signIn()
            .catch(fail)
            .finally(
              () => void client.refetchQueries({ queryKey: ["account"] }).finally(() => setSigningIn(false)),
            )
        },
      },
      children: powerset === "working" ? <Line>Finish signing in to Powerset in your browser.</Line> : null,
    },
    codex: {
      icon: <span className="font-mono text-[12px] font-bold">&gt;_</span>,
      title: "Codex",
      details: details ? <CodexDetails codex={codex} /> : undefined,
      why: "Chat, on your ChatGPT plan.",
      state: codex.loading
        ? "checking"
        : codex.status?.installed && codex.status.account
          ? "ready"
          : codex.login
            ? "working"
            : "needed",
      action: { label: "Connect", onClick: codex.connect, disabled: !codex.status?.installed || locked },
      children: codex.login ? <Line>Finish signing in to ChatGPT in your browser.</Line> : null,
    },
    browser: {
      icon: <Globe className="size-[18px]" />,
      title: "Browser",
      why: "The Chrome profile Powerpacks signs in to LinkedIn and Google with, so it finds your logins and saved passwords.",
      state: chrome.isPending ? "checking" : chrome.data?.linked ? "ready" : "needed",
      children: details ? <BrowserProfiles /> : null,
    },
    imessage: {
      icon: <MessagesIcon className="size-[18px]" />,
      title: "Messages",
      details: details?.imessage,
      why: "iMessage and Contacts, with Full Disk Access.",
      state: ready === undefined ? "checking" : ready.imessage ? "ready" : asking ? "working" : "needed",
      onIcon: onGuide ? () => onGuide("imessage") : undefined,
      action: {
        label: "Connect",
        disabled: locked,
        onClick: () => {
          onGuide?.("imessage")
          setAsking(true)
          setError(null)
        },
      },
      children:
        asking && ready?.imessage === false ? (
          <Line>
            Drag Powerpacks into Full Disk Access, then turn it on. We'll check automatically.{" "}
            <button type="button" className={LINK} onClick={() => setAsking(false)}>
              Skip
            </button>
          </Line>
        ) : null,
    },
    linkedin: source(
      "linkedin",
      "LinkedIn",
      <LinkedInIcon className="size-[18px]" />,
      "Your LinkedIn connections.",
      !linkedin && onBack && (processed || profile !== null) ? (
        <ProfileLink url={profile} onChange={setProfile} />
      ) : null,
    ),
    gmail: source(
      "gmail",
      "Gmail",
      <GmailIcon className="size-[18px]" />,
      <Mailboxes
        known={known}
        email={email}
        disabled={locked}
        onChange={setMailbox}
        onAdd={(added) => {
          onGuide?.("gmail")
          connectGmail([...known, added])
        }}
      />,
    ),
    whatsapp: source(
      "whatsapp",
      "WhatsApp",
      <WhatsAppIcon className="size-[18px]" />,
      "Your WhatsApp chats, by scanning a code.",
    ),
  }

  return (
    <div className={cn("flex w-full flex-col gap-4", layout && "min-h-0 flex-1")}>
      {layout ? (
        layout(items)
      ) : (
        <ul className="m-0 flex list-none flex-col divide-y divide-line overflow-hidden rounded-[var(--radius-m)] border border-line-strong bg-card p-0 text-left">
          {rows
            .filter((key) => shown.has(key))
            .map((key) => (
              <Row key={key} {...items[key]} />
            ))}
        </ul>
      )}
      {onDone ? (
        <div className="flex justify-end">
          <Button variant="primary" disabled={busy} onClick={onDone}>
            {doneLabel}
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
