import type { SVGProps } from "react"

import { cn } from "@/lib/utils"

// Powerset's P, from network-search-app's design/redesign-reference/logo_dark.svg, on its tile.
const P_PATH =
  "M137.777 295.556V102.222H228.429C237.403 102.222 245.461 103.792 252.603 106.931C259.745 109.885 265.789 114.04 270.733 119.395C275.861 124.75 279.799 131.305 282.546 139.061C285.293 146.632 286.666 154.941 286.666 163.989C286.666 173.222 285.293 181.624 282.546 189.194C279.799 196.765 275.861 203.228 270.733 208.583C265.789 213.938 259.745 218.185 252.603 221.324C245.461 224.279 237.403 225.756 228.429 225.756H179.532V295.556H137.777ZM179.532 189.194H223.485C229.711 189.194 234.564 187.625 238.044 184.486C241.707 181.162 243.538 176.361 243.538 170.083V157.896C243.538 151.617 241.707 146.909 238.044 143.769C234.564 140.446 229.711 138.784 223.485 138.784H179.532V189.194Z"

/** The Powerset mark: the orange P on its dark tile. Decorative; its host carries the words. */
export function PowersetMark({ className, ...rest }: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 400 400" aria-hidden className={cn("size-6 shrink-0", className)} {...rest}>
      <rect width="400" height="400" rx="88" fill="#111111" />
      <path d={P_PATH} fill="#F2502A" />
    </svg>
  )
}
