const WARMTH_LEVELS = [1, 2, 3, 4] as const

// Warmth 0-4 as four bars, lit up to the nearest level (the People table, a search result).
export function WarmthBar({ value }: { value: number }) {
  const on = Math.round(value)
  return (
    <span className="warmth-bar">
      {WARMTH_LEVELS.map((level) => (
        <i key={level} className={level <= on ? "on" : ""} />
      ))}
    </span>
  )
}
