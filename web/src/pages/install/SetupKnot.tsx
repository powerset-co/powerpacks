import { AsciiForm } from "@/pages/home/AsciiForm"

/** The home page's knot on setup's screens: drawn in fewer characters, and smaller. */
export function SetupKnot() {
  return <AsciiForm cols={72} rows={36} className="w-[300px] max-w-full" />
}
