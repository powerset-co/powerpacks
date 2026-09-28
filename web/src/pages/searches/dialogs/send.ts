import type { ToastMessage } from "@/components/shared"
import { OUTCOME_MESSAGE } from "@/lib/searches/feedback"
import type { FeedbackRecord } from "@/types/searches"

import type { Feedback } from "../hooks/useFeedback"

/** Where a dialog hands its record: `useFeedback().submit`, and the page's toast. */
export interface FeedbackSink {
  submit: Feedback["submit"]
  onToast: (toast: ToastMessage) => void
}

/** Submits after the dialog has closed and toasts whether the record went out or waits. */
export function send(sink: FeedbackSink, record: FeedbackRecord): void {
  void sink.submit(record).then((outcome) => sink.onToast({ message: OUTCOME_MESSAGE[outcome] }))
}
