// In the desktop app an outbound link opens in the system browser through the app's own
// command, the path every sign-in already takes. WebKit's new-window delegate, which a
// target="_blank" anchor would otherwise rely on, did nothing for a plain link in the app.

import { isDesktop } from "@/lib/desktop"
import { openExternal } from "@/lib/api/install"

/** Whether the click is on a link to another site. */
export function outboundLink(event: MouseEvent): HTMLAnchorElement | null {
  const target = event.target
  if (!(target instanceof Element)) return null
  const anchor = target.closest("a[href]")
  if (!(anchor instanceof HTMLAnchorElement)) return null
  const url = new URL(anchor.href, location.href)
  if (!/^https?:$|^mailto:$/.test(url.protocol)) return null
  if (url.origin === location.origin) return null
  return anchor
}

/** Sends every outbound link click to the system browser; a no-op in a plain browser. */
export function routeExternalLinks(): () => void {
  if (!isDesktop()) return () => undefined
  const onClick = (event: MouseEvent) => {
    if (event.defaultPrevented || event.button !== 0) return
    const anchor = outboundLink(event)
    if (!anchor) return
    event.preventDefault()
    void openExternal(anchor.href)
  }
  document.addEventListener("click", onClick)
  return () => document.removeEventListener("click", onClick)
}
