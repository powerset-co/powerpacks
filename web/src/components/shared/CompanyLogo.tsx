import { cn } from "@/lib/utils"

import { ProxiedImage } from "./ProxiedLinkedinAvatar"

interface CompanyLogoProps {
  company: string
  src?: string
  // 14: the badge on a person's avatar; 24: a role in the drawer's work experience.
  size: 14 | 24
  className?: string
}

const SIZE_CLASS = {
  14: "size-[14px] rounded-[4px]",
  24: "size-6 rounded-md",
} as const

/** A company's logo on a rounded square. Without one (or until it loads) the square stays
 *  empty: no letter or stand-in that could pass for a real logo. */
export function CompanyLogo({ company, src, size, className }: CompanyLogoProps) {
  return (
    <span
      className={cn("relative shrink-0 overflow-hidden bg-line-strong", SIZE_CLASS[size], className)}
      title={company}
      data-company-logo={src ? "image" : "empty"}
    >
      {src ? <ProxiedImage key={src} src={src} /> : null}
    </span>
  )
}
