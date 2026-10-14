import type { HTMLAttributes, ReactNode } from "react"

import { cn } from "@/lib/utils"

const PAGE_WIDTH = "max-w-[960px]"

interface PageHeaderProps extends HTMLAttributes<HTMLElement> {
  title: string
  // The page's controls, at the right edge and centred on the title.
  children?: ReactNode
}

// Every page's title row: one height, gutter and type, so the title never moves between pages.
export function PageHeader({ title, children, className, ...props }: PageHeaderProps) {
  return (
    <header
      className={cn(
        "flex min-h-14 flex-wrap items-center gap-x-4 gap-y-2 px-5 pt-5 max-[680px]:px-4",
        className,
      )}
      {...props}
    >
      <h1 className="m-0 mr-auto text-lg font-semibold leading-7">{title}</h1>
      {children}
    </header>
  )
}

// A page that scrolls as one: the title row and one column under it, both one width, so the
// controls end where the content does.
export function Page({
  title,
  actions,
  children,
}: {
  title: string
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <main className="min-h-0 overflow-y-auto">
      <PageHeader title={title} className={PAGE_WIDTH}>
        {actions}
      </PageHeader>
      <div className={`${PAGE_WIDTH} flex flex-col gap-5 px-5 pb-8 pt-5 max-[680px]:px-4`}>{children}</div>
    </main>
  )
}
