import { useEffect, useState } from "react"

import { thumbnail } from "@/lib/api/thumbnails"
import { cn } from "@/lib/utils"

import { initials } from "./initials"

export type AvatarSize = 26 | 40

const SIZE_CLASS: Record<AvatarSize, string> = {
  26: "size-[26px] basis-[26px] text-[10px]",
  40: "size-10 basis-10 text-xs",
}

interface ProxiedLinkedinAvatarProps {
  name: string
  src?: string
  size: AvatarSize
}

export function ProxiedLinkedinAvatar({ name, src, size }: ProxiedLinkedinAvatarProps) {
  return (
    <span
      className={cn(
        "relative grid shrink-0 grow-0 place-items-center overflow-hidden rounded-full border border-border bg-secondary font-extrabold text-muted-foreground",
        SIZE_CLASS[size],
      )}
    >
      {src ? <AvatarImage key={src} src={src} /> : null}
      <span>{initials(name)}</span>
    </span>
  )
}

// Keyed by src, so a new person's picture starts hidden and fades in over the initials.
function AvatarImage({ src }: { src: string }) {
  const proxied = src.startsWith("https://media.licdn.com/")
  const [resolved, setResolved] = useState<string | undefined>(proxied ? undefined : src)
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    if (!proxied) return
    let active = true
    void thumbnail(src).then((url) => {
      if (active) setResolved(url)
    })
    return () => {
      active = false
    }
  }, [src, proxied])

  if (!resolved) return null
  return (
    <img
      src={resolved}
      alt=""
      loading="lazy"
      referrerPolicy="no-referrer"
      data-loaded={loaded || undefined}
      onLoad={() => setLoaded(true)}
      onError={() => setLoaded(false)}
      className={cn(
        "absolute inset-0 z-[1] size-full object-cover opacity-0 transition-opacity duration-fast ease-out",
        loaded && "opacity-100",
      )}
    />
  )
}
