// Copied from the cloud app (network-search-app `constants/companyFilters.ts`) for the Edit Filters modal.
import type { EntityItem } from "./expandedFilters"

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
}

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
}

// Funding stages in order (for range selection)
// Backend uses integer mapping: 1=pre_seed, 2=seed, 3=series_a, etc.
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
}

// Convert to EntityItem arrays for autocomplete
export const ENTITY_TYPE_OPTIONS: EntityItem[] = Object.entries(ENTITY_TYPES).map(([id, display_value]) => ({
  id,
  display_value,
  type: "entity_type",
  score: 0,
  metadata: null,
}))

export const SECTOR_TYPE_OPTIONS: EntityItem[] = Object.entries(SECTOR_TYPES).map(([id, display_value]) => ({
  id,
  display_value,
  type: "sector_type",
  score: 0,
  metadata: null,
}))

// Seniority bands for role leveling
export const SENIORITY_BANDS: EntityItem[] = [
  { id: "trainee", display_value: "Trainee", type: "seniority_band", score: 0, metadata: null },
  { id: "entry", display_value: "Entry", type: "seniority_band", score: 0, metadata: null },
  { id: "junior", display_value: "Junior", type: "seniority_band", score: 0, metadata: null },
  { id: "mid", display_value: "Mid", type: "seniority_band", score: 0, metadata: null },
  { id: "senior", display_value: "Senior", type: "seniority_band", score: 0, metadata: null },
  { id: "staff", display_value: "Staff", type: "seniority_band", score: 0, metadata: null },
  { id: "principal", display_value: "Principal", type: "seniority_band", score: 0, metadata: null },
  { id: "manager", display_value: "Manager", type: "seniority_band", score: 0, metadata: null },
  { id: "director", display_value: "Director", type: "seniority_band", score: 0, metadata: null },
  { id: "vice-president", display_value: "Vice President", type: "seniority_band", score: 0, metadata: null },
  { id: "c-suite", display_value: "C-Suite", type: "seniority_band", score: 0, metadata: null },
  { id: "partner", display_value: "Partner", type: "seniority_band", score: 0, metadata: null },
  { id: "owner", display_value: "Owner", type: "seniority_band", score: 0, metadata: null },
]

// Fuzzy search function for client-side matching
export function fuzzySearch(query: string, options: EntityItem[]): EntityItem[] {
  const lowerQuery = query.toLowerCase().trim()
  if (!lowerQuery) return options.slice(0, 10)

  // Score each option based on match quality
  const scored = options.map((option) => {
    const lowerValue = option.display_value.toLowerCase()
    const lowerId = option.id.toLowerCase()

    let score = 0

    // Exact match (highest priority)
    if (lowerValue === lowerQuery || lowerId === lowerQuery) {
      score = 100
    }
    // Starts with query
    else if (lowerValue.startsWith(lowerQuery) || lowerId.startsWith(lowerQuery)) {
      score = 80
    }
    // Contains query as a word
    else if (lowerValue.includes(` ${lowerQuery}`) || lowerValue.includes(`${lowerQuery} `)) {
      score = 60
    }
    // Contains query anywhere
    else if (lowerValue.includes(lowerQuery) || lowerId.includes(lowerQuery)) {
      score = 40
    }
    // Fuzzy match - check if all characters appear in order
    else {
      let queryIdx = 0
      for (const char of lowerValue) {
        if (queryIdx < lowerQuery.length && char === lowerQuery[queryIdx]) {
          queryIdx++
        }
      }
      if (queryIdx === lowerQuery.length) {
        score = 20
      }
    }

    return { option, score }
  })

  return scored
    .filter((s) => s.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, 10)
    .map((s) => s.option)
}
