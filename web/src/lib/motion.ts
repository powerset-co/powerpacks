// The motion tokens (styles/index.css) read back for Web Animations.

/** A CSS time in milliseconds: "200ms" and ".2s" (the build minifies ms to s) are both 200. */
export function cssMs(value: string): number {
  const time = value.trim()
  const number = parseFloat(time)
  return time.endsWith("ms") ? number : number * 1000
}

export interface Motion {
  duration: number
  easing: string
}

/** --t-med and --ease-out as they apply at `element`. */
export function motionTokens(element: Element): Motion {
  const style = getComputedStyle(element)
  return {
    duration: cssMs(style.getPropertyValue("--t-med")),
    easing: style.getPropertyValue("--ease-out").trim(),
  }
}
