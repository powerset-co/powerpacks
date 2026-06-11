import path from "path";
import fs from "fs";

import {
  messagesChatDbPath,
  powerpacksRepoRoot,
  whatsAppStorePath,
  whatsAppWacliQrHtmlPath,
  whatsAppWacliQrHtmlRelativePath,
  whatsAppWacliQrPngPath,
  whatsAppWacliQrPngRelativePath,
  whatsAppWahaEngine,
  whatsAppWahaImage,
  whatsAppWahaQrPngPath,
  whatsAppWahaQrPngRelativePath,
  whatsAppWahaQrTxtPath,
  whatsAppWahaQrTxtRelativePath,
} from "./paths";
import { setupProcessEnv } from "./env";
import { accountRecords } from "./accounts";
import type { RunState } from "./types";

export const SETUP_SOURCE_ORDER = ["gmail", "linkedin_csv", "messages", "twitter"] as const;
export const SETUP_SOURCE_LABELS: Record<string, string> = {
  gmail: "Gmail",
  linkedin_csv: "LinkedIn",
  messages: "Messages",
  twitter: "Twitter/X",
};

let cachedWhatsAppLinkStatus: { expiresAt: number; value: Record<string, any> } | null = null;

export function clearWhatsAppLinkStatusCache() {
  cachedWhatsAppLinkStatus = null;
}

export function whatsAppProvider(): "wacli" | "waha" {
  const value = String(setupProcessEnv().POWERPACKS_WHATSAPP_PROVIDER || "wacli").trim().toLowerCase();
  return value === "waha" ? "waha" : "wacli";
}

export function whatsAppQrPngRelativePath(): string {
  return whatsAppProvider() === "waha" ? whatsAppWahaQrPngRelativePath : whatsAppWacliQrPngRelativePath;
}

export function imessagePermissionStatus(): Record<string, any> {
  const base = {
    status: "permission_required",
    chat_db: messagesChatDbPath,
    exists: false,
    readable: false,
    permission_required: true,
    message: "Full Disk Access is required to read Messages chat.db.",
  };
  if (!messagesChatDbPath) {
    return { ...base, error: "HOME is not set, so Messages chat.db could not be located." };
  }
  if (!fs.existsSync(messagesChatDbPath)) {
    return { ...base, error: "Messages chat.db was not found." };
  }
  try {
    const fd = fs.openSync(messagesChatDbPath, "r");
    fs.closeSync(fd);
    return {
      ...base,
      status: "ready",
      exists: true,
      readable: true,
      permission_required: false,
      message: "Messages chat.db is readable.",
    };
  } catch (err) {
    return {
      ...base,
      exists: true,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}

export function whatsappLinkStatus(config: Record<string, any> = {}): Record<string, any> {
  const nowMs = Date.now();
  if (cachedWhatsAppLinkStatus && cachedWhatsAppLinkStatus.expiresAt > nowMs) {
    return cachedWhatsAppLinkStatus.value;
  }

  const provider = whatsAppProvider();
  const configuredStore = String(config.store || whatsAppStorePath);
  const storePath = path.isAbsolute(configuredStore) ? configuredStore : path.join(powerpacksRepoRoot, configuredStore);
  const qrPage = fs.existsSync(whatsAppWacliQrHtmlPath) ? whatsAppWacliQrHtmlRelativePath : "";
  const qrPng = fs.existsSync(whatsAppWacliQrPngPath) ? whatsAppWacliQrPngRelativePath : "";
  const qrUpdatedAt = fs.existsSync(whatsAppWacliQrPngPath) ? fs.statSync(whatsAppWacliQrPngPath).mtime.toISOString() : "";
  const authenticated = config.authenticated === true || ["linked", "authenticated"].includes(String(config.status || ""));
  const base = {
    status: authenticated ? "authenticated" : "not_authenticated",
    authenticated,
    provider,
    engine: provider === "waha" ? whatsAppWahaEngine : undefined,
    image: provider === "waha" ? whatsAppWahaImage : undefined,
    store: provider === "wacli" ? configuredStore : undefined,
    store_exists: fs.existsSync(storePath),
    qr_page: authenticated ? "" : qrPage,
    qr_png: authenticated ? "" : qrPng,
    qr_updated_at: authenticated ? "" : qrUpdatedAt,
  };
  const value: Record<string, any> = provider === "waha" && !authenticated ? {
    ...base,
    qr_png: fs.existsSync(whatsAppWahaQrPngPath) ? whatsAppWahaQrPngRelativePath : "",
    qr_raw: fs.existsSync(whatsAppWahaQrTxtPath) ? whatsAppWahaQrTxtRelativePath : "",
    qr_updated_at: fs.existsSync(whatsAppWahaQrPngPath) ? fs.statSync(whatsAppWahaQrPngPath).mtime.toISOString() : "",
  } : base;
  cachedWhatsAppLinkStatus = { expiresAt: nowMs + 5000, value };
  return value;
}

export function messagesLinkStatus(config: Record<string, any> = {}): Record<string, Record<string, any>> {
  const whatsappConfig = config.whatsapp && typeof config.whatsapp === "object" ? config.whatsapp as Record<string, any> : {};
  return {
    imessage: imessagePermissionStatus(),
    whatsapp: whatsappLinkStatus(whatsappConfig),
  };
}

export function normalizeSetupSources(accounts: RunState | null) {
  const records = accountRecords(accounts);
  const messagesRecord = records.messages || {};
  const messagesConfig = messagesRecord.config && typeof messagesRecord.config === "object" ? messagesRecord.config : {};
  const messagesStatus = messagesLinkStatus(messagesConfig);
  return SETUP_SOURCE_ORDER.map((id) => {
    const record = records[id] || {};
    const linked = Boolean(record.linked || record.status === "linked");
    const skipped = Boolean(record.skipped || record.status === "skipped");
    const baseConfig = record.config && typeof record.config === "object" ? record.config : {};
    const config = id === "messages"
      ? {
        ...baseConfig,
        imessage: {
          ...((baseConfig.imessage && typeof baseConfig.imessage === "object") ? baseConfig.imessage : {}),
          ...messagesStatus.imessage,
        },
        whatsapp: {
          ...((baseConfig.whatsapp && typeof baseConfig.whatsapp === "object") ? baseConfig.whatsapp : {}),
          ...messagesStatus.whatsapp,
        },
      }
      : baseConfig;
    const liveStatus = id === "messages" && !skipped
      ? (messagesStatus.imessage.status === "ready" ? (linked ? "linked" : "ready") : "permission_required")
      : null;
    return {
      id,
      label: SETUP_SOURCE_LABELS[id],
      status: skipped ? "skipped" : liveStatus || (linked ? "linked" : record.status || "unlinked"),
      linked,
      skipped,
      usernames: Array.isArray(record.usernames) ? record.usernames.map(String) : [],
      artifacts: Array.isArray(record.artifacts) ? record.artifacts.map(String) : [],
      notes: record.notes || "",
      lastCheckedAt: record.last_checked_at || record.lastCheckedAt || null,
      lastSuccessAt: record.last_success_at || record.lastSuccessAt || null,
      config,
    };
  });
}

export function sourceSlug(value: string): string {
  return (value || "source").toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[._-]+|[._-]+$/g, "") || "source";
}
