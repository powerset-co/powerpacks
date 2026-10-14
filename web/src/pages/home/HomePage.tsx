import { Link } from "react-router-dom"

import { Button } from "@/components/ui/button"
import { isDesktop } from "@/lib/desktop"

import { AsciiForm } from "./AsciiForm"

export function HomePage() {
  return (
    <main className="page-enter flex min-h-0 flex-col items-center justify-center overflow-y-auto px-6 py-10 text-center">
      <AsciiForm />
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
    </main>
  )
}
