import { cva } from "class-variance-authority"

// people.css .source: "sm" is the table pill, "md" the larger one for looser layouts. The
// LinkedIn glyph is drawn two steps larger so its filled square reads as heavy as the outlines.
export const sourcePillVariants = cva("source inline-flex items-center justify-center rounded-full border", {
  variants: {
    size: {
      sm: "h-5 px-[7px] [&_svg]:size-[11px] [&[data-c=linkedin]_svg]:size-[13px]",
      md: "h-6 px-2 [&_svg]:size-3.5 [&[data-c=linkedin]_svg]:size-4",
    },
  },
  defaultVariants: {
    size: "sm",
  },
})
