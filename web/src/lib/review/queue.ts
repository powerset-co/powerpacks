// Browse the ordered review history without deciding.

import type { QueuePosition } from "@/types/review"

export type CarouselDirection = "previous" | "next"

/** The position one press away; the queue wraps both ways. */
export function carouselIndex({ index, total }: QueuePosition, direction: CarouselDirection): number {
  const size = Math.max(1, total)
  return (index + (direction === "next" ? 1 : size - 1)) % size
}
