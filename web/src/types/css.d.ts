// CSS custom properties in React style objects: style={{ "--row-h": "36px" }}.
import "react"

declare module "react" {
  // An interface: a module augmentation merges, a type alias cannot.
  interface CSSProperties {
    [property: `--${string}`]: string | number | undefined
  }
}
