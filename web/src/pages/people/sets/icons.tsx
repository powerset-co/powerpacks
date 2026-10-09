/** A local network (the personal set) and a cloud set. */
export function HomeIcon({ className = "" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" aria-hidden="true">
      <path
        d="M2.5 7.5 8 3l5.5 4.5V13h-4v-3.5h-3V13h-4z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function CloudIcon({ className = "" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" aria-hidden="true">
      <path
        d="M4.5 12.5h7a3 3 0 0 0 .4-5.97A4 4 0 0 0 4.2 7.6 2.5 2.5 0 0 0 4.5 12.5z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
    </svg>
  )
}
