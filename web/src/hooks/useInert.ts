import { useEffect, type RefObject } from "react"

/** Keeps the element inert while `inert` holds: a shut or leaving part takes no focus or clicks. */
export function useInert(ref: RefObject<HTMLElement>, inert: boolean): void {
  useEffect(() => {
    if (ref.current) ref.current.inert = inert
  }, [ref, inert])
}
