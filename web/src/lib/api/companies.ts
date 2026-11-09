// The Companies page's two routes on the local server: name suggestions and the company search,
// each for one scope (the local index or a Powerset set).

import { body, failure } from "@/lib/api/http"
import type {
  CompanySearchRequest,
  CompanySearchResponse,
  CompanyScope,
  CompanySuggestion,
} from "@/types/companies"

export async function suggestCompanies(q: string, scope: CompanyScope): Promise<CompanySuggestion[]> {
  const params = new URLSearchParams({ q, scope })
  const response = await fetch(`/api/companies/suggest?${params.toString()}`)
  if (!response.ok) throw await failure(response, "Couldn't load suggestions.")
  return (await body<{ suggestions: CompanySuggestion[] }>(response)).suggestions
}

export async function searchCompanies(request: CompanySearchRequest): Promise<CompanySearchResponse> {
  const response = await fetch("/api/companies/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  })
  if (!response.ok) throw await failure(response, "Couldn't search companies.")
  return body<CompanySearchResponse>(response)
}
