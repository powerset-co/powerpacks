import { useCallback, useEffect, useRef } from "react"

/**
 * Whether the stage has left the screen, asked after a wait on the server. A stage that left
 * does nothing more with a late answer: on the old page the document was gone by then, and
 * here a late stage check or reload would move the screen the user has since opened.
 */
export function useLeftScreen(): () => boolean {
  const onScreen = useRef(true)
  useEffect(() => {
    onScreen.current = true
    return () => {
      onScreen.current = false
    }
  }, [])
  return useCallback(() => !onScreen.current, [])
}
