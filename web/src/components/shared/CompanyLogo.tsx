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
  14: "size-[14px] rounded-[4px] text-[8px]",
  24: "size-6 rounded-md text-[11px]",
} as const

/** A company's logo on a rounded square, over the company's first letter until it loads (or
 *  when there is none). */
export function CompanyLogo({ company, src, size, className }: CompanyLogoProps) {
  const letter = company.trim().charAt(0).toUpperCase()
  return (
    <span
      className={cn(
        "relative grid shrink-0 place-items-center overflow-hidden bg-line-strong font-extrabold leading-none text-foreground",
        SIZE_CLASS[size],
        className,
      )}
      title={company}
      data-company-logo={src ? "image" : "letter"}
    >
      {src ? <ProxiedImage key={src} src={src} /> : null}
      <span aria-hidden="true">{letter}</span>
    </span>
  )
}
