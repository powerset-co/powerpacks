import { cva } from "class-variance-authority"

// people.css .source: "sm" is the table pill, "md" the larger one for looser layouts. The
// LinkedIn and X glyphs are drawn two steps larger so their filled marks read as heavy as the
// outlines. A count (results.css .network-source) sits after the glyph in tabular figures.
export const sourcePillVariants = cva(
  "source inline-flex items-center justify-center gap-1 rounded-full border text-[10px] font-semibold tabular-nums",
  {
    variants: {
      size: {
        sm: "h-5 px-[7px] [&_svg]:size-[11px] [&[data-c=linkedin]_svg]:size-[13px] [&[data-c=x]_svg]:size-[13px]",
        md: "h-6 px-2 [&_svg]:size-3.5 [&[data-c=linkedin]_svg]:size-4 [&[data-c=x]_svg]:size-4",
      },
    },
    defaultVariants: {
      size: "sm",
    },
  },
)
