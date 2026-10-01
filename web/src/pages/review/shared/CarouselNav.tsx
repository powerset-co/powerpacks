import { CAROUSEL } from "@/lib/review/copy"
import { carouselIndex, type CarouselDirection } from "@/lib/review/queue"
import type { QueuePosition } from "@/types/review"

interface CarouselNavProps {
  /** Where the card on screen sits in its queue. */
  queue: QueuePosition
  /** The position to read next; the stage fetches that card. Nothing is written. */
  onIndex: (index: number) => void
}

// templates/carousel_nav.html.j2 (`?debug=1`): Previous and Next around the card, wrapping
// at both ends.
export function CarouselNav({ queue, onIndex }: CarouselNavProps) {
  const go = (direction: CarouselDirection) => onIndex(carouselIndex(queue, direction))
  return (
    <>
      <button
        type="button"
        className="carousel-nav carousel-prev"
        aria-label={CAROUSEL.previous}
        onClick={() => go("previous")}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="m15 5-7 7 7 7" />
        </svg>
      </button>
      <button
        type="button"
        className="carousel-nav carousel-next"
        aria-label={CAROUSEL.next}
        onClick={() => go("next")}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="m9 5 7 7-7 7" />
        </svg>
      </button>
    </>
  )
}
