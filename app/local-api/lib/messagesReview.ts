import path from "path";
import fs from "fs";
import { spawnSync } from "child_process";

import {
  messagesLedgerPath,
  messagesReviewCsvPath,
  powerpacksRepoRoot,
  powerpacksStateRoot,
  safeJoinPowerpacks,
} from "./paths";
import { readJsonSync } from "./fsUtils";
import { setupProcessEnv } from "./env";

export function resolveReviewCsvPath(): string | null {
  if (fs.existsSync(messagesReviewCsvPath)) return messagesReviewCsvPath;
  const ledger = readJsonSync(messagesLedgerPath) || {};
  const artifactPath = ledger.artifacts?.research_review_csv || ledger.artifacts?.review_csv;
  const resolved = safeJoinPowerpacks(artifactPath);
  if (resolved && fs.existsSync(resolved)) return resolved;
  return fs.existsSync(messagesReviewCsvPath) ? messagesReviewCsvPath : null;
}

export function messagesReviewPrimitive(command: string, args: string[] = []): Record<string, any> {
  const csvPath = resolveReviewCsvPath() || messagesReviewCsvPath;
  const result = spawnSync("uv", [
    "run", "--project", ".", "python",
    "packs/messages/primitives/review_research_web/review_research_web.py",
    command,
    "--csv", csvPath,
    "--research-dir", path.join(powerpacksStateRoot, "messages", "research"),
    ...args,
  ], {
    cwd: powerpacksRepoRoot,
    env: setupProcessEnv(),
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  });
  if (result.status !== 0) {
    throw new Error((result.stderr || result.stdout || `review primitive failed: ${command}`).trim());
  }
  return JSON.parse(result.stdout || "{}");
}

export function messagesReviewResponse(filter = "all", query = "", offset = 0, limit = 100): Record<string, any> {
  return messagesReviewPrimitive("json", [
    "--filter", filter,
    "--query", query,
    "--offset", String(offset),
    "--limit", String(limit),
  ]);
}

export function messagesCurrentBlockForUi(messagesLedger: Record<string, any>, reviewCounts: Record<string, any>) {
  const block = messagesLedger.current_block || null;
  if (!block) return null;
  const approvalType = String(block.approval_type || "").trim().toLowerCase();
  if (approvalType === "upload") return null;
  if (approvalType !== "parallel") {
    if (block.review_url) {
      return {
        ...block,
        review_url: "/setup/imessage/review",
        message: "Review Messages contacts in the inline setup app. Click Complete when done.",
      };
    }
    return block;
  }

  const prepareInput = String(messagesLedger.steps?.prepare_research_queue?.summary?.input || "");
  const selectedForResearch = Number(reviewCounts.researchSelected || 0);

  // Once the setup app owns review decisions, approvals from the old
  // contacts.csv queue are stale. Completing review recomputes the queue from
  // research_review.csv and only then can a fresh Parallel approval be shown.
  if (prepareInput && !prepareInput.includes("research_review.csv")) return null;
  if (selectedForResearch === 0) return null;
  return block;
}
