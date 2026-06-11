// API client for the /local-api/companies endpoints. Do not move these
// helpers into powerpacksApi.ts.

export type CompaniesSortField =
  | "current_people"
  | "total_people"
  | "company_name"
  | "headcount"
  | "founded_year";
export type CompaniesSortDir = "asc" | "desc";

export interface LocalCompanyRow {
  id: string;
  company_name: string;
  aliases: string[];
  description: string | null;
  website_domain: string | null;
  linkedin_url: string | null;
  logo_url: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  metro_area: string | null;
  entity_types: string[];
  sector_types: string[];
  customer_type: string[];
  headcount: number | null;
  funding_stage: number | null;
  funding_total: number | null;
  stage: string | null;
  founded_year: number | null;
  current_people: number;
  total_people: number;
}

export interface LocalCompaniesResponse {
  rows: LocalCompanyRow[];
  total: number;
  page: number;
  page_size: number;
  index_missing?: boolean;
}

export interface LocalCompanyPerson {
  person_id: string;
  full_name: string | null;
  headline: string | null;
  profile_picture_url: string | null;
  city: string | null;
  state: string | null;
  linkedin_url: string | null;
  position_title: string | null;
  is_current: boolean;
  start_date_epoch: number | null;
  end_date_epoch: number | null;
  seniority_band: string | null;
}

export interface LocalCompanyDetailsResponse {
  company: LocalCompanyRow;
  people: LocalCompanyPerson[];
}

export interface FetchLocalCompaniesOptions {
  q?: string;
  sort?: CompaniesSortField;
  dir?: CompaniesSortDir;
  page?: number;
  pageSize?: number;
}

export async function fetchLocalCompanies(options: FetchLocalCompaniesOptions = {}): Promise<LocalCompaniesResponse> {
  const params = new URLSearchParams();
  if (options.q) params.set("q", options.q);
  if (options.sort) params.set("sort", options.sort);
  if (options.dir) params.set("dir", options.dir);
  if (options.page != null && options.page > 0) params.set("page", String(options.page));
  if (options.pageSize != null) params.set("page_size", String(options.pageSize));
  const query = params.toString() ? `?${params.toString()}` : "";

  const response = await fetch(`/local-api/companies${query}`);
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`/local-api/companies failed: ${response.status} ${text}`);
  }
  return response.json() as Promise<LocalCompaniesResponse>;
}

export async function fetchLocalCompanyDetails(companyId: string): Promise<LocalCompanyDetailsResponse | null> {
  const response = await fetch(`/local-api/companies/${encodeURIComponent(companyId)}`);
  if (response.status === 404) return null;
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`/local-api/companies/${companyId} failed: ${response.status} ${text}`);
  }
  return response.json() as Promise<LocalCompanyDetailsResponse>;
}

// ── Display label maps ───────────────────────────────────────────────
// Copied from network-search-app src/constants/companyFilters.ts so local
// company badges render the same human-readable labels as prod.

export const ENTITY_TYPES: Record<string, string> = {
  venture_backed_startup: "Venture-Backed Startup",
  vc_firm: "VC Firm",
  pe_firm: "PE Firm",
  family_office: "Family Office",
  sovereign_wealth_fund: "Sovereign Wealth Fund",
  bank: "Bank",
  foundation_endowment: "Foundation/Endowment",
  insurance_carrier: "Insurance Carrier",
  nonprofit: "Nonprofit",
  government_public_sector: "Government/Public Sector",
  club_association: "Club/Association",
};

export const SECTOR_TYPES: Record<string, string> = {
  saas: "SaaS",
  infra_devtools: "Infrastructure Software & Developer Tools",
  devops: "DevOps",
  data: "Data",
  physical_compute: "Physical Compute Infrastructure",
  ai_ml: "Artificial Intelligence & Machine Learning",
  cybersecurity: "Cybersecurity",
  iot: "Internet of Things (IoT)",
  fintech: "Fintech",
  crypto: "Crypto",
  insurtech: "InsurTech",
  mortgagetech: "Mortgage Tech",
  hardware: "Hardware",
  semiconductors: "Semiconductors",
  deep_tech: "Deep Tech",
  robotics_drones: "Robotics & Drones",
  aerospace: "Aerospace",
  "3d_printing": "3D Printing",
  material_science: "Material Science",
  networking_hardware: "Networking Hardware",
  telco: "Telco",
  health_tech: "Health Tech",
  therapies: "Therapies",
  diagnostics: "Diagnostics",
  bio_synbio: "Bio/Synbio",
  oncology: "Oncology",
  medical_devices: "Medical Devices",
  real_estate_tech: "Real Estate Tech",
  construction_tech: "Construction Tech",
  manufacturing_tech: "Manufacturing Tech",
  supply_chain_logistics: "Supply Chain & Logistics Tech",
  transportation_mobility: "Transportation & Mobility",
  climate_energy_tech: "Climate & Energy Tech",
  oil_gas_tech: "Oil & Gas Tech",
  agriculture_tech: "Agriculture & FoodTech",
  defense_tech: "Defense Tech",
  travel_tech: "Travel Tech",
  restaurant_hospitality_tech: "Restaurant & Hospitality Tech",
  commerce_tech: "Commerce Tech",
  marketplaces: "Marketplaces",
  hr_tech: "HR Tech",
  sales_tech: "Sales Tech",
  marketing_tech: "Marketing & Advertising Tech",
  legal_tech: "Legal Tech",
  edtech: "EdTech",
  govtech: "GovTech",
  nonprofit_philanthropy_tech: "Nonprofit & Philanthropy Tech",
  social_networking: "Social Networking",
  dating: "Dating",
  creator_tools: "Creator Tools",
  gaming_gambling_tech: "Gambling & Gaming Tech",
  ar_vr: "AR/VR",
  sports_wellness_tech: "Sports and Health & Wellness Tech",
};

// Backend funding_stage is a 1-based index into this order
// (1=pre_seed, 2=seed, 3=series_a, ...), mirroring prod's integer mapping.
export const FUNDING_STAGE_ORDER: string[] = [
  "pre_seed",
  "seed",
  "series_a",
  "series_b",
  "series_c",
  "series_d",
  "series_e",
  "series_f",
  "series_g",
  "series_h",
  "ipo",
  "exited",
];

export const FUNDING_STAGES: Record<string, string> = {
  pre_seed: "Pre-Seed",
  seed: "Seed",
  series_a: "Series A",
  series_b: "Series B",
  series_c: "Series C",
  series_d: "Series D",
  series_e: "Series E",
  series_f: "Series F",
  series_g: "Series G",
  series_h: "Series H+",
  ipo: "IPO",
  exited: "Exited/Acquired",
};

// Fallback for ids missing from the maps (the local index has a few sector
// ids that prod's map doesn't cover, e.g. "climate_energy").
function humanizeId(id: string): string {
  return id
    .split("_")
    .map((word) => (word ? word[0].toUpperCase() + word.slice(1) : word))
    .join(" ");
}

export function sectorLabel(id: string): string {
  return SECTOR_TYPES[id] || humanizeId(id);
}

export function entityLabel(id: string): string {
  return ENTITY_TYPES[id] || humanizeId(id);
}

export function fundingStageLabel(company: Pick<LocalCompanyRow, "stage" | "funding_stage">): string | null {
  if (company.stage) return FUNDING_STAGES[company.stage] || humanizeId(company.stage);
  if (company.funding_stage != null) {
    const id = FUNDING_STAGE_ORDER[company.funding_stage - 1];
    if (id) return FUNDING_STAGES[id];
  }
  return null;
}
