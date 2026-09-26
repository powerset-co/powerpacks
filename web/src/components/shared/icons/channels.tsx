import type { SVGProps } from "react";

import type { Channel } from "@/types/people";

// The CHANNEL glyphs from the legacy people.js, one per source family.

type IconProps = SVGProps<SVGSVGElement>;

const STROKED = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

const FILLED = { viewBox: "0 0 24 24", fill: "currentColor", stroke: "none" } as const;

export function GmailIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <rect width="20" height="16" x="2" y="4" rx="2" />
      <path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7" />
    </svg>
  );
}

export function IMessageIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z" />
    </svg>
  );
}

export function WhatsAppIcon(props: IconProps) {
  return (
    <svg {...STROKED} {...props}>
      <path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z" />
      <path d="M9 10a3 3 0 0 0 6 4" />
    </svg>
  );
}

export function LinkedInIcon(props: IconProps) {
  return (
    <svg {...FILLED} {...props}>
      <path d="M20.5 2h-17A1.5 1.5 0 002 3.5v17A1.5 1.5 0 003.5 22h17a1.5 1.5 0 001.5-1.5v-17A1.5 1.5 0 0020.5 2zM8 19H5v-9h3zM6.5 8.25A1.75 1.75 0 118.3 6.5a1.78 1.78 0 01-1.8 1.75zM19 19h-3v-4.74c0-1.42-.6-1.93-1.38-1.93A1.74 1.74 0 0013 14.19a.66.66 0 000 .14V19h-3v-9h2.9v1.3a3.11 3.11 0 012.7-1.4c1.55 0 3.36.86 3.36 3.66z" />
    </svg>
  );
}

export const CHANNEL_ICON: Record<Channel, (props: IconProps) => JSX.Element> = {
  gmail: GmailIcon,
  imessage: IMessageIcon,
  whatsapp: WhatsAppIcon,
  linkedin: LinkedInIcon,
};
