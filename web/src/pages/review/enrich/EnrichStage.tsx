import "../styles/enrich.css"

import { EmptyPanel } from "../shared/EmptyPanel"
import { NOTE, TITLE } from "./copy"

// The Enrich stage: the agent runs the enrichment, and this screen only waits. The page's server
// watch reads the status every few seconds and moves on to LinkedIn when the store does.
export function EnrichStage() {
  return (
    <EmptyPanel
      title={TITLE}
      className="enrich-state"
      above={<span className="enrich-shape" aria-hidden="true" />}
    >
      <p>{NOTE}</p>
    </EmptyPanel>
  )
}
