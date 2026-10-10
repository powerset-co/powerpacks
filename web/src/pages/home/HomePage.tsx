import { Link } from "react-router-dom"

import { Button } from "@/components/ui/button"
import { readStored } from "@/lib/storage"
import { FORMS, KNOT } from "./forms"
import { isDesktop } from "@/lib/desktop"

import { AsciiForm } from "./AsciiForm"

export function HomePage() {
  const form =
    readStored("local", "home.form", (name) => FORMS.find((entry) => entry.name === name) ?? null) ?? KNOT
  return (
    <main className="page-enter flex min-h-0 flex-col items-center justify-center overflow-y-auto px-6 py-10 text-center">
      <AsciiForm form={form} />
      <h1 className="m-0 text-3xl font-semibold tracking-tight">Small world. Infinite possibilities.</h1>
      <p className="mb-7 mt-3 max-w-sm text-sm leading-relaxed text-muted-foreground">
        Find the right person. Pick up a conversation. Make something happen.
      </p>
      <div className="flex flex-wrap justify-center gap-3">
        <Button asChild variant="primary">
          <Link to={isDesktop() ? "/agent" : "/searches"}>Search your network</Link>
        </Button>
        <Button asChild variant="default">
          <Link to="/people">Explore people</Link>
        </Button>
      </div>
      <Link to="/home/variations" className="mt-5 text-xs text-muted-foreground underline underline-offset-4">
        Try 14 variations
      </Link>
    </main>
  )
}
