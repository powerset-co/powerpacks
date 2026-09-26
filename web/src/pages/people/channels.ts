import { CHANNEL_TITLE } from "@/lib/people/copy";
import type { Channel } from "@/types/people";

// The row's source names, keeping only the families that have a pill.
export function toChannels(values: readonly string[]): Channel[] {
  return values.filter((value): value is Channel => value in CHANNEL_TITLE);
}
