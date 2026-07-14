import path from "path";

export const appRoot = path.resolve(__dirname, "..", "..");
export const powerpacksRepoRoot = path.resolve(
  appRoot,
  process.env.POWERPACKS_REPO_ROOT || ".."
);
export const powerpacksStateRoot = path.join(powerpacksRepoRoot, ".powerpacks");
export const discoverContactsSetupLedger = ".powerpacks/network-import/discover/ledger.setup.json";
export const runsDir = path.join(powerpacksStateRoot, "runs");
export const onboardingV2LinkedInRunsDir = path.join(powerpacksStateRoot, "runs", "setup-linkedin-csv");
export const onboardingV2GmailRunsDir = path.join(powerpacksStateRoot, "runs", "setup-gmail");
export const onboardingV3LinkedInRunsDir = path.join(powerpacksStateRoot, "runs", "setup-linkedin-modal");
export const onboardingV3GmailRunsDir = path.join(powerpacksStateRoot, "runs", "setup-gmail-modal");
export const setupLedgerPath = path.join(powerpacksStateRoot, "setup", "setup-run.json");
export const accountsPath = path.join(powerpacksStateRoot, "ingestion", "accounts.json");
export const importRefreshLedgerPath = path.join(powerpacksRepoRoot, discoverContactsSetupLedger);
export const messagesDiscoveryManifestPath = path.join(powerpacksStateRoot, "network-import", "discover", "messages", "manifest.json");
export const messagesReviewCsvPath = path.join(powerpacksStateRoot, "messages", "research_review.csv");

export function safeJoinPowerpacks(relativePath: string | undefined | null): string | null {
  if (!relativePath) return null;
  const resolved = path.resolve(powerpacksRepoRoot, relativePath);
  if (!resolved.startsWith(powerpacksRepoRoot)) return null;
  return resolved;
}
