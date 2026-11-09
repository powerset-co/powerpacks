// The Companies page's request and response: the cloud's /v2/companies/search shape
// (network-search-app companySearchServiceV2.ts), which the local server answers in kind for the
// local index (packs/shared/web/companies.py). Only the fields the page reads.

/** "local", or a Powerset set id. */
export type CompanyScope = string

export interface CompanySearchRequest {
  scope: CompanyScope
  id?: string
  name?: string
  include_people?: boolean
  position_type: "current" | "all"
  people_limit?: number
  people_offset?: number
  people_sort?: "current" | "name"
  people_dir?: "asc" | "desc"
  limit: number
  offset?: number
}

export interface CompanyPerson {
  id: string
  name: string
  public_identifier: string | null
  position_title: string
  is_current: boolean
  tenure_years: number | null
}

export interface Company {
  id: string
  name: string
  headcount: number | null
  city: string | null
  state: string | null
  country: string | null
  logo_url: string | null
  people_count: number
  people_offset?: number
  people_has_more?: boolean
  people?: CompanyPerson[]
}

export interface CompanySearchResponse {
  companies: Company[]
  total: number
  limit: number
  offset: number
}

export interface CompanySuggestion {
  id: string
  display_value: string
}
