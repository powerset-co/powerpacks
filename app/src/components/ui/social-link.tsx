import { forwardRef } from "react";
import { Linkedin } from "lucide-react";
import { XIcon } from "@/components/icons/XIcon";
import { cn } from "@/lib/utils";

interface SocialLinkProps extends React.ComponentPropsWithoutRef<"a"> {
  href: string;
  className?: string;
  /** Icon size in pixels (default: 14) */
  size?: number;
}

/**
 * LinkedIn profile link — blue icon, opens in new tab, stops propagation.
 */
export const LinkedinLink = forwardRef<HTMLAnchorElement, SocialLinkProps>(
  ({ href, className, size = 14, onClick, ...props }, ref) => {
    return (
      <a
        ref={ref}
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(e) => {
          e.stopPropagation();
          onClick?.(e);
        }}
        className={cn("inline-flex items-center relative shrink-0 text-[#0A66C2] hover:text-[#004182] transition-colors", className)}
        {...props}
      >
        <Linkedin style={{ width: size, height: size }} />
      </a>
    );
  }
);
LinkedinLink.displayName = "LinkedinLink";

/**
 * X/Twitter profile link — dark icon, opens in new tab, stops propagation.
 * Use everywhere a Twitter/X handle is rendered as a clickable icon.
 */
export const XLink = forwardRef<HTMLAnchorElement, SocialLinkProps>(
  ({ href, className, size = 14, onClick, ...props }, ref) => {
    return (
      <a
        ref={ref}
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(e) => {
          e.stopPropagation();
          onClick?.(e);
        }}
        className={cn("inline-flex items-center text-foreground/60 hover:text-foreground transition-colors shrink-0", className)}
        {...props}
      >
        <XIcon size={size} />
      </a>
    );
  }
);
XLink.displayName = "XLink";

interface SocialLinksProps {
  linkedinUrl?: string | null;
  xTwitterHandle?: string | null;
  className?: string;
  /** Icon size in pixels (default: 14) */
  size?: number;
}

/**
 * Compact social icons row — LinkedIn + X side by side.
 * Renders only icons that have data. Use everywhere you show a person's name.
 */
export function SocialLinks({ linkedinUrl, xTwitterHandle, className, size = 14 }: SocialLinksProps) {
  // Filter out synthetic LinkedIn URLs (synth-* profiles don't have real LinkedIn pages)
  const hasLinkedin = !!linkedinUrl && !linkedinUrl.includes("/synth-");
  const hasX = !!xTwitterHandle;

  if (!hasLinkedin && !hasX) return null;

  const xUrl = xTwitterHandle?.startsWith("http")
    ? xTwitterHandle
    : `https://x.com/${xTwitterHandle?.replace(/^@/, "")}`;

  return (
    <span className={cn("inline-flex items-center gap-1.5 shrink-0", className)}>
      {hasLinkedin && <LinkedinLink href={linkedinUrl!} size={size} />}
      {hasX && <XLink href={xUrl} size={size} />}
    </span>
  );
}
