// The review page's URLs. The query names are `stage`, `view` (the worth tab), `preview`,
// `debug` and `index`.

import type { ReviewView, WorthTab } from "@/types/review"

export const REVIEW_PATH = "/"

/** What the address bar asks for. `stage` and `view` go to the server as written: it decides
 *  the screen (GET /api/review/page). */
export interface ScreenQuery {
  stage: string
  view: string
  /** The user opened this screen deliberately; the feed-forward never moves it. */
  preview: boolean
  /** The queue carousel. */
  debug: boolean
  /** The queue position to open on; 0 unless the URL names one. */
  index: number
}

const ON = "1"

export function readScreenQuery(search: string): ScreenQuery {
  const params = new URLSearchParams(search)
  const index = Number.parseInt(params.get("index") ?? "", 10)
  return {
    stage: params.get("stage") ?? "",
    view: params.get("view") ?? "",
    preview: params.get("preview") === ON,
    debug: params.get("debug") === ON,
    index: Number.isNaN(index) ? 0 : Math.max(0, index),
  }
}

/** A stage's screen, as a stage transition opens it: no preview, the default tab. */
export function stageHref(stage: ReviewView): string {
  return `${REVIEW_PATH}?stage=${stage}`
}

/** A stepper step: the stage, opened deliberately. */
export function stepHref(stage: ReviewView): string {
  return `${stageHref(stage)}&preview=1`
}

/** A worth tab; a deliberately opened screen stays one. */
export function tabHref(tab: WorthTab, preview: boolean): string {
  return `${stageHref("worth")}&view=${tab}${preview ? "&preview=1" : ""}`
}
