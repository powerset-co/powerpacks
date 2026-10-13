import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState, type FormEvent, type ReactNode } from "react"

import { CHANNEL_ICON, CheckIcon, PowersetMark, Spinner } from "@/components/shared"
import { Button } from "@/components/ui/button"
import { useCodexAccount } from "@/lib/agent/useCodexAccount"
import { fetchAccount } from "@/lib/api/account"
import { signIn } from "@/lib/api/feedback"
import { errorText } from "@/lib/api/http"
import {
  connectSource,
  continueSetup,
  fetchConnection,
  fetchPreflight,
  installAction,
  installPreflight,
  messagesReadable,
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

// While a sign-in, an install or macOS is still answering, ask again this often.
const POLL_MS = 2_000

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

/** One thing to connect: what it is for, then a button, a spinner or a check. */
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
          className="w-28 shrink-0 self-center"
        >
          {action.label}
        </Button>
      ) : (
        <span
          role="img"
          aria-label={state === "ready" ? `${title} connected` : `${title} working`}
          className={cn(
            "rise-in grid size-7 shrink-0 place-items-center self-center rounded-full",
            state === "ready" ? "bg-ok-soft text-ok" : "text-faint",
          )}
        >
          {state === "ready" ? <CheckIcon className="size-3.5" /> : <Spinner />}
        </span>
      )}
    </li>
  )
}

/** A line under a row's purpose: what its step is doing, or why it stopped. */
function Line({ children, bad = false }: { children: ReactNode; bad?: boolean }) {
  return <span className={cn("text-xs leading-snug", bad ? "text-bad" : "text-foreground")}>{children}</span>
}

/** Where one source stands in the Connect screen's run. */
function standing(record: InstallStatus | undefined, source: ConnectSource) {
  if (record === undefined) return { state: "checking" as const }
  const current = STEPS[source].includes(record.step)
  const live = (record.installer_pid ?? 0) > 0
  if (current && (record.status === "running" || (record.status === "waiting" && live)))
    return { state: "working" as const, line: record.message, qr: record.action?.qr_url }
  // Stopped: failed, or waiting with nothing left running (a password, a closed window).
  if (current && (record.status === "failed" || record.status === "waiting"))
    return { state: "needed" as const, line: record.message, command: record.action?.command, stopped: true }
  const login = STEPS[source][STEPS[source].length - 1] ?? ""
  return { state: record.steps[login]?.status === "completed" ? ("ready" as const) : ("needed" as const) }
}

/** Whether macOS lets Powerpacks read Messages, asked again while not yet; null until it answers. */
function useMessagesReadable(): boolean | null {
  const [readable, setReadable] = useState<boolean | null>(null)
  useEffect(() => {
    let stopped = false
    const check = async () => {
      const allowed = await messagesReadable()
      if (stopped) return
      setReadable(allowed)
      if (allowed) stopped = true
    }
    const timer = setInterval(() => void check(), POLL_MS)
    void check()
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [])
  return readable
}

/** The Gmail mailbox: the Powerset address until another is typed. */
function Mailbox({ email, onChange }: { email: string; onChange: (email: string) => void }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(email)
  if (!editing)
    return (
      <>
        From {email || "your Google account"}
        {" · "}
        <button
          type="button"
          className="cursor-pointer border-0 bg-transparent p-0 text-xs text-primary hover:underline"
          onClick={() => {
            setValue(email)
            setEditing(true)
          }}
        >
          Change
        </button>
      </>
    )
  const save = (event: FormEvent) => {
    event.preventDefault()
    if (value.includes("@")) onChange(value.trim())
    setEditing(false)
  }
  return (
    <form onSubmit={save} className="flex gap-2">
      <input
        type="email"
        aria-label="Gmail address"
        ref={(input) => input?.focus()}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onBlur={save}
        placeholder="you@gmail.com"
        className="h-7 min-w-0 flex-1 rounded-[var(--radius-s)] border border-line-strong bg-card px-2 text-xs text-foreground outline-none focus-visible:border-[color-mix(in_srgb,var(--primary)_55%,var(--line-strong))]"
      />
    </form>
  )
}

/** The desktop app's setup before it starts: every sign-in, one row each, so setup itself never
 *  waits on the user. Only Powerset is needed; a source left unconnected is left out of setup.
 *  Full Disk Access comes before the browser sign-ins: turning it on makes macOS restart the app. */
export function Connect({ onBack }: { onBack: () => void }) {
  const client = useQueryClient()
  const codex = useCodexAccount()
  const readable = useMessagesReadable()
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [mailbox, setMailbox] = useState<string | null>(null)
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
  // One sign-in at a time: setup's own lock holds the others off.
  const busy =
    queued !== null ||
    (record !== undefined &&
      (record.status === "running" || (record.status === "waiting" && (record.installer_pid ?? 0) > 0)))
  const email = mailbox ?? account.data?.email ?? ""

  const fail = (caught: unknown) => setError(errorText(caught))
  // Install what the sign-in needs (a browser, Google's tools), wait for it, then start it.
  const connect = async (source: ConnectSource) => {
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
      await connectSource(source, source === "gmail" ? email : undefined)
      await client.invalidateQueries({ queryKey: ["install", "connect"] })
    } catch (caught) {
      fail(caught)
    } finally {
      setQueued(null)
    }
  }

  const source = (name: ConnectSource, title: string, icon: ReactNode, why: ReactNode, label: string) => {
    const at =
      queued === name
        ? { state: "working" as const, line: "Installing what it needs…" }
        : standing(record, name)
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
          label: "stopped" in at ? "Try again" : label,
          onClick: () => void connect(name),
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
      </Row>
    )
  }

  return (
    <div className="flex w-full flex-col gap-4">
      <ul className="m-0 flex list-none flex-col divide-y divide-line rounded-[var(--radius-m)] border border-line-strong bg-card p-0 text-left">
        <Row
          icon={<PowersetMark className="size-[18px]" />}
          title="Powerset"
          why={signedIn ? (account.data?.email ?? "") : "Your account. Needed for setup."}
          state={account.isPending ? "checking" : signedIn ? "ready" : "needed"}
          action={{
            label: "Sign in",
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
          why="Runs Chat on your ChatGPT plan."
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
          why="Full Disk Access, for iMessage and Contacts."
          state={readable === null ? "checking" : readable ? "ready" : "needed"}
          action={{ label: "Open Settings", onClick: () => void installAction("permissions").catch(fail) }}
        />
        {source(
          "linkedin",
          "LinkedIn",
          <LinkedInIcon className="size-[18px]" />,
          "Your connections. Sign in once in the window that opens.",
          "Sign in",
        )}
        {source(
          "gmail",
          "Gmail",
          <GmailIcon className="size-[18px]" />,
          <Mailbox email={email} onChange={setMailbox} />,
          "Connect",
        )}
        {source(
          "whatsapp",
          "WhatsApp",
          <WhatsAppIcon className="size-[18px]" />,
          "Scan a code with your phone: Linked devices, then Link a device.",
          "Link",
        )}
      </ul>
      <div className="flex items-center justify-between gap-3">
        <Button disabled={starting} onClick={onBack}>
          Back
        </Button>
        <span className="text-xs text-muted-foreground">Anything left unconnected is skipped.</span>
        <Button
          variant="primary"
          disabled={!signedIn || busy || starting}
          onClick={() => {
            setStarting(true)
            setError(null)
            void continueSetup({}).catch((caught: unknown) => {
              fail(caught)
              setStarting(false)
            })
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
