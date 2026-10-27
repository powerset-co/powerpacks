import { useEffect, useState } from "react"

import { cn } from "@/lib/utils"

// Until the picture is there, ask again this often.
const POLL_MS = 3_000
const URL = "/api/install/linkedin-page"

/** The picture of the connections page the LinkedIn read took as it started, in a window of its
 *  own: the person sees their own list is what is being read. Asked for until it is there. */
export function PageShot({ caption, reading }: { caption: string; reading: boolean }) {
  const [src, setSrc] = useState(() => `${URL}?t=${Date.now()}`)
  const [shown, setShown] = useState(false)
  useEffect(() => {
    if (shown) return
    const timer = setInterval(() => setSrc(`${URL}?t=${Date.now()}`), POLL_MS)
    return () => clearInterval(timer)
  }, [shown])
  return (
    <figure className="m-0 flex flex-col gap-3">
      <div
        className={cn(
          "overflow-hidden rounded-[var(--radius-m)] border border-line-strong bg-background shadow-[var(--shadow-2)] transition-opacity duration-slow ease-out",
          shown ? "opacity-100" : "opacity-0",
        )}
        aria-hidden={!shown}
      >
        <div className="flex h-7 items-center gap-1.5 border-b border-line bg-card px-3">
          <span className="size-2 rounded-full bg-line-strong" />
          <span className="size-2 rounded-full bg-line-strong" />
          <span className="size-2 rounded-full bg-line-strong" />
          <span className="ml-2 truncate text-[10px] text-faint">
            linkedin.com/mynetwork/invite-connect/connections
          </span>
        </div>
        <div className="relative">
          <img
            src={src}
            alt="Your LinkedIn connections page, as Powerpacks reads it"
            className="block w-full"
            onLoad={() => setShown(true)}
            onError={() => setShown(false)}
          />
          {/* A sweep over the page while the read is on. */}
          {reading && shown ? (
            <div
              aria-hidden
              className="pointer-events-none absolute inset-0 -translate-x-full animate-[shimmer_2.4s_var(--ease-in)_infinite] bg-[linear-gradient(90deg,transparent,color-mix(in_srgb,var(--primary)_16%,transparent),transparent)] motion-reduce:hidden"
            />
          ) : null}
        </div>
      </div>
      <figcaption
        className={cn(
          "text-xs text-muted-foreground transition-opacity",
          shown ? "opacity-100" : "opacity-0",
        )}
      >
        {caption}
      </figcaption>
    </figure>
  )
}
