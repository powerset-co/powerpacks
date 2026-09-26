import { forwardRef, type InputHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

type SearchFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type">;

// results.css .field: the one text box style, here as a search input.
export const SearchField = forwardRef<HTMLInputElement, SearchFieldProps>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    type="search"
    className={cn(
      "h-8 rounded-sm border border-border bg-card px-2.5 text-[12.5px] text-foreground outline-none transition-[border-color] duration-fast ease-out placeholder:text-faint focus:border-primary",
      className,
    )}
    {...props}
  />
));
SearchField.displayName = "SearchField";
