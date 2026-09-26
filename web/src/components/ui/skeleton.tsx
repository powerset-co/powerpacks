import { cn } from "@/lib/utils"

function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("relative overflow-hidden rounded-sm bg-card after:absolute after:inset-0 after:-translate-x-full after:bg-[linear-gradient(90deg,transparent,rgba(255,255,255,.05),transparent)] after:content-[''] after:animate-[shimmer_1.3s_infinite]", className)}
      {...props}
    />
  )
}

export { Skeleton }
