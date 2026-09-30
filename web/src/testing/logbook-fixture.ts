// Synthetic Logbook statuses, every key present as GET /api/people/logbook sends them.

import type { LogbookResult, LogbookStatus } from "@/lib/api/logbook"

export const RESULT: LogbookResult = {
  root: ".powerpacks/logbook",
  entries: ["casey-delta-p2", "family"],
  messages: 12,
  files: 3,
  channels: [
    { channel: "gmail", status: "missing", messages: 0, earliest: null, latest: null },
    { channel: "imessage", status: "unreadable", messages: 0, earliest: null, latest: null },
    {
      channel: "whatsapp",
      status: "ok",
      messages: 12,
      earliest: "2015-03-01T00:00:00Z",
      latest: "2026-09-01T00:00:00Z",
    },
  ],
  elapsed_seconds: 0.4,
}

export function logbookStatus(overrides: Partial<LogbookStatus> = {}): LogbookStatus {
  return { status: "idle", people: [], result: null, error: null, ...overrides }
}
