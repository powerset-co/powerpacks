import { Link } from "react-router-dom"

import { PowersetMark } from "@/components/shared/PowersetMark"
import { Button } from "@/components/ui/button"
import { isDesktop } from "@/lib/desktop"

import "./home.css"

const POINTS = [
  [74, 140],
  [160, 60],
  [315, 74],
  [410, 156],
  [384, 294],
  [270, 348],
  [132, 318],
  [44, 246],
] as const

export function HomePage() {
  return (
    <main className="page-enter flex min-h-0 flex-col items-center justify-center overflow-y-auto px-6 py-10 text-center">
      <div className="relative aspect-[6/5] w-full max-w-[480px] shrink-0" aria-hidden>
        <svg viewBox="0 0 480 400" className="home-network absolute inset-0 size-full text-primary">
          {POINTS.map(([x, y], index) => (
            <g key={x} style={{ animationDelay: `${index * -1.2}s` }}>
              <path
                d={`M240 200 L${x} ${y}`}
                pathLength="1"
                fill="none"
                stroke="currentColor"
                strokeOpacity=".22"
              />
              <circle cx={x} cy={y} r={index % 3 === 0 ? 5 : 3} fill="currentColor" opacity=".7" />
              <circle cx={x} cy={y} r="12" fill="currentColor" opacity=".05" />
            </g>
          ))}
          <circle cx="240" cy="200" r="116" fill="none" stroke="currentColor" strokeOpacity=".06" />
          <circle cx="240" cy="200" r="172" fill="none" stroke="currentColor" strokeOpacity=".04" />
        </svg>
        <PowersetMark className="absolute left-1/2 top-1/2 size-20 -translate-x-1/2 -translate-y-1/2 rounded-2xl shadow-[var(--shadow-2)]" />
      </div>
      <h1 className="m-0 text-3xl font-semibold tracking-tight">A world of people you know.</h1>
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
