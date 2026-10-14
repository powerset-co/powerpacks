import { Component, type ErrorInfo, type ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { invoke, isDesktop } from "@/lib/desktop"

interface Props {
  children: ReactNode
  /** Where the user is: moving on clears the error, without remounting what did not throw. */
  at: string
}

/** A page that throws shows what to do instead of a blank window; the desktop app has Codex look
 *  into it (crash.rs) and file the report. */
export class ErrorBoundary extends Component<Props, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidUpdate(previous: Props) {
    if (this.state.failed && previous.at !== this.props.at) this.setState({ failed: false })
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    if (!isDesktop()) return
    const detail = [window.location.pathname, error.stack ?? "", info.componentStack ?? ""].join("\n")
    invoke("report_crash", { summary: `${error.name}: ${error.message}`, detail }).catch(() => undefined)
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <main className="grid place-items-center px-5">
        <div className="flex flex-col items-center gap-3 text-center">
          <p className="m-0 text-muted-foreground">This page hit an error. Reload to try again.</p>
          <Button variant="primary" onClick={() => window.location.reload()}>
            Reload
          </Button>
        </div>
      </main>
    )
  }
}
