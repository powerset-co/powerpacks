import path from "path";

import { powerpacksRepoRoot } from "../lib/paths";
import { readEnvSummary } from "../lib/env";
import { fileSummary } from "../lib/fsUtils";
import { sendJson } from "../lib/http";

type EnvKeySpec = {
  key: string;
  label: string;
  provider: string;
  description: string;
  required: boolean;
  getUrl: string;
  docsUrl?: string;
  aliases?: string[];
};

const ENV_KEY_SPECS: EnvKeySpec[] = [
  {
    key: "OPENAI_API_KEY",
    label: "OpenAI API key",
    provider: "OpenAI",
    description: "Used for local search reranking, embeddings, and indexing enrichment.",
    required: true,
    getUrl: "https://platform.openai.com/api-keys",
  },
  {
    key: "RAPIDAPI_LINKEDIN_KEY",
    label: "RapidAPI LinkedIn key",
    provider: "RapidAPI",
    description: "Used for LinkedIn profile hydration during import and enrichment. RAPIDAPI_KEY is accepted as a legacy fallback.",
    required: true,
    getUrl: "https://rapidapi.com/pnd-team-pnd-team/api/professional-network-data",
    aliases: ["RAPIDAPI_KEY"],
  },
  {
    key: "PARALLEL_API_KEY",
    label: "Parallel API key",
    provider: "Parallel",
    description: "Used for Gmail and Messages LinkedIn resolution / deep research.",
    required: true,
    getUrl: "https://platform.parallel.ai/settings?tab=api-keys",
  },
  {
    key: "APOLLO_API_KEY",
    label: "Apollo API key",
    provider: "Apollo",
    description: "Used for Apollo-backed contact and enrichment workflows.",
    required: true,
    getUrl: "https://developer.apollo.io/keys#/keys",
  },
  {
    key: "TURBOPUFFER_API_KEY",
    label: "TurboPuffer API key",
    provider: "TurboPuffer",
    description: "Used by cloud-backed search primitives when local DuckDB is not selected.",
    required: false,
    getUrl: "https://turbopuffer.com",
  },
  {
    key: "DATABASE_URL",
    label: "Database URL",
    provider: "Postgres",
    description: "Used by cloud-backed search and operator bootstrap exports.",
    required: false,
    getUrl: "https://supabase.com/dashboard",
    aliases: ["SUPABASE_DATABASE_URL", "SUPABASE_DB_URL"],
  },
  {
    key: "RAPIDAPI_TWITTER_KEY",
    label: "RapidAPI Twitter/X key",
    provider: "RapidAPI",
    description: "Used only for Twitter/X import; RAPIDAPI_KEY is accepted as a legacy fallback.",
    required: false,
    getUrl: "https://rapidapi.com/hub",
    aliases: ["RAPIDAPI_KEY"],
  },
];

function maskEnvValue(value: string): string {
  if (!value) return "";
  if (value.length <= 8) return "set";
  return `${value.slice(0, 4)}...${value.slice(-4)}`;
}

function envValueStatus(value: unknown): "present" | "empty" | "missing" {
  if (value === undefined) return "missing";
  return String(value || "").trim() ? "present" : "empty";
}

function envStatus() {
  const envPath = path.join(powerpacksRepoRoot, ".env");
  const env = readEnvSummary();
  const file = fileSummary(envPath);
  const keys = ENV_KEY_SPECS.map((spec) => {
    const primaryValue = env[spec.key];
    const primaryStatus = envValueStatus(primaryValue);
    const aliasMatches = (spec.aliases || []).map((alias) => ({
      key: alias,
      status: envValueStatus(env[alias]),
      valuePreview: envValueStatus(env[alias]) === "present" ? maskEnvValue(String(env[alias])) : "",
    }));
    const satisfiedAlias = aliasMatches.find((alias) => alias.status === "present");
    const satisfied = primaryStatus === "present" || Boolean(satisfiedAlias);
    const status = primaryStatus === "present"
      ? "present"
      : satisfiedAlias
        ? "present_via_alias"
        : primaryStatus;
    return {
      key: spec.key,
      label: spec.label,
      provider: spec.provider,
      description: spec.description,
      required: spec.required,
      getUrl: spec.getUrl,
      docsUrl: spec.docsUrl || "",
      aliases: aliasMatches,
      status,
      satisfied,
      satisfiedBy: primaryStatus === "present" ? spec.key : satisfiedAlias?.key || "",
      valuePreview: primaryStatus === "present" ? maskEnvValue(String(primaryValue)) : "",
    };
  });
  const required = keys.filter((key) => key.required);
  const missingRequired = required.filter((key) => !key.satisfied);
  return {
    ...file,
    keys,
    summary: {
      total: keys.length,
      required: required.length,
      ready: missingRequired.length === 0,
      missingRequired: missingRequired.length,
      present: keys.filter((key) => key.satisfied).length,
      empty: keys.filter((key) => key.status === "empty").length,
      missing: keys.filter((key) => key.status === "missing").length,
    },
  };
}

export async function handleEnvRoutes(req: any, res: any, url: URL): Promise<boolean> {
  if (url.pathname === "/local-api/env/status") {
    sendJson(res, envStatus());
    return true;
  }

  return false;
}
