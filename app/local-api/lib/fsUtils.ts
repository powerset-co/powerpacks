import path from "path";
import fs from "fs";
import { createHash } from "crypto";

import { powerpacksRepoRoot } from "./paths";
import type { RunState } from "./types";

export function readJsonSync(filePath: string): RunState | null {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

export function writeJsonSync(filePath: string, data: unknown) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmp = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`, "utf8");
  fs.renameSync(tmp, filePath);
}

export function fileSummary(filePath: string) {
  if (!fs.existsSync(filePath)) {
    return { path: path.relative(powerpacksRepoRoot, filePath), exists: false };
  }
  const stat = fs.statSync(filePath);
  return {
    path: path.relative(powerpacksRepoRoot, filePath),
    exists: true,
    updatedAt: stat.mtime.toISOString(),
    sizeBytes: stat.size,
  };
}

export function removeLocalFiles(paths: string[]): string[] {
  const removed: string[] = [];
  for (const filePath of paths) {
    try {
      fs.unlinkSync(filePath);
      removed.push(path.relative(powerpacksRepoRoot, filePath));
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== "ENOENT") throw err;
    }
  }
  return removed;
}

export function sha256File(filePath: string): string {
  try {
    return createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
  } catch {
    return "";
  }
}

export function readRelativeLedger(relativePath: string): RunState {
  return readJsonSync(path.join(powerpacksRepoRoot, relativePath)) || {};
}
