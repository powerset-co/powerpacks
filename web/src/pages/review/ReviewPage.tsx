// The shared styles load before any stage's own (ReviewScreen imports the stages).
import "./styles/base.css"

import { useEffect } from "react"
import { Link } from "react-router-dom"

import { Toast } from "@/components/shared"
import { BRAND, documentTitle, LOAD_FAILED } from "@/lib/review/copy"
import { stageHref } from "@/lib/review/links"

import { useReviewToast } from "./hooks/useReviewToast"
import { useScreen } from "./hooks/useScreen"
import { ReviewScreen } from "./ReviewScreen"
import { EmptyPanel } from "./shared/EmptyPanel"

// The old page's toast sat bottom-centre in 13px; the shared Toast is bottom-right in 12px.
const TOAST_PLACE = "inset-x-0 bottom-6 mx-auto w-fit max-w-[calc(100vw-32px)] text-[13px]"

/**
 * /review: the deep-context review flow (worth, Enrich, LinkedIn, done), one screen per URL
 * (`stage`, `view`, `preview`, `debug`, `index`). The page is the user's entry point and
 * stands outside the app shell: its own top bar (brand and the screen's title), the stepper,
 * the stage, and the one toast every control reports to.
 */
export function ReviewPage() {
  const { screen, error, reload, open } = useScreen()
  const toast = useReviewToast()
  const title = screen?.page.title

  useEffect(() => {
    if (title) document.title = documentTitle(title)
  }, [title])

  // A new screen starts without the last one's toast, as a document load cleared it.
  const { dismiss } = toast
  const screenId = screen?.id
  useEffect(() => dismiss(), [dismiss, screenId])

  return (
    <div className="review-page" data-stage={screen?.page.view} data-preview={screen?.preview}>
      <header className="topbar">
        <Link className="brand" to={stageHref("worth")}>
          {BRAND}
        </Link>
        <h1 className="topbar-title">{title}</h1>
        <span />
      </header>
      {error ? (
        <main className="review-main">
          <EmptyPanel title={LOAD_FAILED}>
            <p>{error.message}</p>
          </EmptyPanel>
        </main>
      ) : screen ? (
        <ReviewScreen key={screen.id} screen={screen} toast={toast} reload={reload} open={open} />
      ) : (
        <main className="review-main" aria-busy="true" />
      )}
      <Toast toast={toast.toast} onDismiss={toast.dismiss} className={TOAST_PLACE} />
    </div>
  )
}
