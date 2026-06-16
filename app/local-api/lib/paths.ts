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
export const onboardingV2MessagesRunsDir = path.join(powerpacksStateRoot, "runs", "setup-messages");
export const onboardingV3LinkedInRunsDir = path.join(powerpacksStateRoot, "runs", "setup-linkedin-modal");
export const onboardingV3GmailRunsDir = path.join(powerpacksStateRoot, "runs", "setup-gmail-modal");
export const setupLedgerPath = path.join(powerpacksStateRoot, "setup", "setup-run.json");
export const accountsPath = path.join(powerpacksStateRoot, "ingestion", "accounts.json");
export const importRefreshLedgerPath = path.join(powerpacksRepoRoot, discoverContactsSetupLedger);
export const messagesLedgerPath = path.join(powerpacksStateRoot, "messages", "import-run.setup-messages.json");
export const messagesReviewCsvPath = path.join(powerpacksStateRoot, "messages", "research_review.csv");
export const whatsAppWacliQrPngRelativePath = ".powerpacks/messages/wacli-login-qr.png";
export const whatsAppWacliQrHtmlRelativePath = ".powerpacks/messages/wacli-login-qr.html";
export const whatsAppWacliQrPngPath = path.join(powerpacksStateRoot, "messages", "wacli-login-qr.png");
export const whatsAppWacliQrHtmlPath = path.join(powerpacksStateRoot, "messages", "wacli-login-qr.html");
export const whatsAppWahaQrPngRelativePath = ".powerpacks/messages/whatsapp/qr.png";
export const whatsAppWahaQrTxtRelativePath = ".powerpacks/messages/whatsapp/qr.txt";
export const whatsAppWahaQrPngPath = path.join(powerpacksStateRoot, "messages", "whatsapp", "qr.png");
export const whatsAppWahaQrTxtPath = path.join(powerpacksStateRoot, "messages", "whatsapp", "qr.txt");
export const whatsAppWahaEngine = "NOWEB";
export const whatsAppWahaImage = "devlikeapro/waha:noweb-2026.3.4";
export const messagesChatDbPath = process.env.POWERPACKS_IMESSAGE_CHAT_DB
  ? path.resolve(process.env.POWERPACKS_IMESSAGE_CHAT_DB)
  : path.join(process.env.HOME || "", "Library", "Messages", "chat.db");
export const whatsAppStorePath = ".powerpacks/messages/wacli";

export function safeJoinPowerpacks(relativePath: string | undefined | null): string | null {
  if (!relativePath) return null;
  const resolved = path.resolve(powerpacksRepoRoot, relativePath);
  if (!resolved.startsWith(powerpacksRepoRoot)) return null;
  return resolved;
}
