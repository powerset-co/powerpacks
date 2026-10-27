import { useEffect, useState } from "react"

import { cn } from "@/lib/utils"

// Until the picture is there, ask again this often.
const POLL_MS = 3_000

/** A picture of a page a run is on, in a window of its own: the LinkedIn connections page as the
 *  read starts, or Google Cloud as the Gmail app is set up. Asked for until it is there; while
 *  `reading`, asked for again every few seconds, since the run keeps taking new ones. */
export function PageShot({
  url,
  address,
  caption,
  reading,
}: {
  url: string
  address: string
  caption: string
  reading: boolean
}) {
  const [src, setSrc] = useState(() => `${url}?t=${Date.now()}`)
  const [shown, setShown] = useState(false)
  useEffect(() => {
    if (shown && !reading) return
    const timer = setInterval(() => setSrc(`${url}?t=${Date.now()}`), POLL_MS)
    return () => clearInterval(timer)
  }, [shown, reading, url])
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
          <span className="ml-2 truncate text-[10px] text-faint">{address}</span>
        </div>
        <div className="relative">
          <img
            src={src}
            alt={caption}
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
