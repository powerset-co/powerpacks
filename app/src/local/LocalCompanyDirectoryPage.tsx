import React, { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowLeft, ArrowUp, ChevronDown, ChevronLeft, ChevronRight, Loader2, Search } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { SocialLinks } from "@/components/ui/social-link";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetchCompanies, fetchCompanyDetail } from "./powerpacksApi";
import type { Company, PeopleSortDir, PeopleSortField } from "./types";

const PAGE_SIZE = 100;
const PEOPLE_PAGE_SIZE = 50;

function getDefaultPeopleSortDir(field: PeopleSortField): PeopleSortDir {
  return field === "current" ? "desc" : "asc";
}

function splitFilter(value: string): string[] {
  return value.split(",").map((part) => part.trim()).filter(Boolean);
}

function formatMoney(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "-";
  if (Math.abs(value) >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(1)}B`;
  if (Math.abs(value) >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`;
  return `$${value.toLocaleString()}`;
}

function locationText(company: Company): string {
  return [company.city, company.state, company.country].filter(Boolean).join(", ");
}

function uniqueValues(companies: Company[], key: "sector_types" | "entity_types"): string[] {
  return Array.from(new Set(companies.flatMap((company) => company[key] || []).filter(Boolean))).sort((a, b) => a.localeCompare(b));
}

export default function LocalCompanyDirectoryPage() {
  const [name, setName] = useState("");
  const [sectorText, setSectorText] = useState("");
  const [entityText, setEntityText] = useState("");
  const [companies, setCompanies] = useState<Company[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [offset, setOffset] = useState(0);
  const [source, setSource] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [selectedCompany, setSelectedCompany] = useState<Company | null>(null);
  const [isLoadingPeople, setIsLoadingPeople] = useState(false);
  const [peopleQuery, setPeopleQuery] = useState("");
  const [peoplePage, setPeoplePage] = useState(0);
  const [peopleSortBy, setPeopleSortBy] = useState<PeopleSortField>("current");
  const [peopleSortDir, setPeopleSortDir] = useState<PeopleSortDir>("desc");
  const [expandedPersonIds, setExpandedPersonIds] = useState<Set<string>>(new Set());

  const loadCompanies = useCallback(async (nextOffset = 0) => {
    setIsSearching(true);
    setError(null);
    try {
      const response = await fetchCompanies({
        name: name || undefined,
        sector_types: splitFilter(sectorText),
        entity_types: splitFilter(entityText),
        limit: PAGE_SIZE,
        offset: nextOffset,
      });
      setCompanies(response.companies || []);
      setTotalCount(response.total || 0);
      setOffset(response.offset ?? nextOffset);
      setSource(response.source || null);
      setWarnings(response.warnings || []);
      setSelectedCompany(null);
    } catch (err) {
      console.error("Company search failed", err);
      setError(err instanceof Error ? err.message : "Company search failed");
      setCompanies([]);
      setTotalCount(0);
    } finally {
      setIsSearching(false);
    }
  }, [entityText, name, sectorText]);

  useEffect(() => {
    loadCompanies(0);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const loadCompanyDetail = useCallback(async (company: Company, page = 0, sortBy = peopleSortBy, sortDir = peopleSortDir) => {
    setIsLoadingPeople(true);
    setError(null);
    setExpandedPersonIds(new Set());
    try {
      const response = await fetchCompanyDetail(company.id, {
        include_people: true,
        people_limit: PEOPLE_PAGE_SIZE,
        people_offset: page * PEOPLE_PAGE_SIZE,
        people_sort: sortBy,
        people_dir: sortDir,
        people_search: peopleQuery || undefined,
      });
      setSelectedCompany(response.company || company);
      setPeoplePage(page);
      setSource(response.source || source);
      setWarnings(response.warnings || []);
    } catch (err) {
      console.error("Company detail failed", err);
      setSelectedCompany(company);
      setError(err instanceof Error ? err.message : "Could not load company detail");
    } finally {
      setIsLoadingPeople(false);
    }
  }, [peopleQuery, peopleSortBy, peopleSortDir, source]);

  const sectorOptions = useMemo(() => uniqueValues(companies, "sector_types"), [companies]);
  const entityOptions = useMemo(() => uniqueValues(companies, "entity_types"), [companies]);
  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  if (selectedCompany) {
    const people = selectedCompany.people || [];
    const peopleTotalCount = selectedCompany.people_count ?? people.length;
    const peopleTotalPages = Math.max(1, Math.ceil(peopleTotalCount / PEOPLE_PAGE_SIZE));
    const peopleStart = selectedCompany.people_offset ?? peoplePage * PEOPLE_PAGE_SIZE;
    const peopleHasPrev = peopleStart > 0;
    const peopleHasNext = selectedCompany.people_has_more ?? peopleStart + people.length < peopleTotalCount;

    const handlePeopleSort = (field: PeopleSortField) => {
      const defaultDir = getDefaultPeopleSortDir(field);
      const nextDir = peopleSortBy === field ? (peopleSortDir === "asc" ? "desc" : "asc") : defaultDir;
      setPeopleSortBy(field);
      setPeopleSortDir(nextDir);
      loadCompanyDetail(selectedCompany, 0, field, nextDir);
    };

    const SortIcon = ({ field }: { field: PeopleSortField }) => {
      if (peopleSortBy !== field) return null;
      return peopleSortDir === "asc" ? <ArrowUp className="ml-1 inline h-3 w-3" /> : <ArrowDown className="ml-1 inline h-3 w-3" />;
    };

    return (
      <div className="container mx-auto max-w-6xl space-y-6 overflow-auto px-4 py-8">
        <Button variant="ghost" onClick={() => { setSelectedCompany(null); setPeopleQuery(""); setExpandedPersonIds(new Set()); }} className="gap-2">
          <ArrowLeft className="h-4 w-4" />
          Back to company results
        </Button>

        {error && <Card className="border-destructive/40 p-4 text-sm text-destructive">{error}</Card>}

        <Card className="space-y-4 p-6">
          <div className="flex items-start gap-4">
            {selectedCompany.logo_url && <img src={selectedCompany.logo_url} alt={selectedCompany.name} className="h-16 w-16 rounded-lg bg-muted object-contain" />}
            <div className="flex-1 space-y-2">
              <h1 className="text-2xl font-bold">{selectedCompany.name}</h1>
              {locationText(selectedCompany) && <p className="text-sm text-muted-foreground">{locationText(selectedCompany)}</p>}
              <div className="flex flex-wrap gap-2">
                {(selectedCompany.entity_types || []).map((entity) => <Badge key={entity} variant="outline" className="text-xs">{entity}</Badge>)}
                {(selectedCompany.sector_types || []).slice(0, 8).map((sector) => <Badge key={sector} variant="secondary" className="text-xs">{sector}</Badge>)}
              </div>
            </div>
            <div className="text-right text-sm text-muted-foreground">
              <div>Headcount: {selectedCompany.headcount?.toLocaleString() ?? "-"}</div>
              <div>People in DB: {selectedCompany.people_count?.toLocaleString() ?? "-"}</div>
              <div>Funding: {formatMoney(selectedCompany.funding_total)}</div>
            </div>
          </div>
          {selectedCompany.description && <p className="text-sm text-muted-foreground">{selectedCompany.description}</p>}
          <div className="flex flex-wrap items-center gap-3 text-sm">
            {selectedCompany.stage && <Badge variant="outline">Stage: {selectedCompany.stage}</Badge>}
            {selectedCompany.linkedin_url && <a className="text-primary hover:underline" href={selectedCompany.linkedin_url} target="_blank" rel="noreferrer">LinkedIn</a>}
          </div>
        </Card>

        <Card className="p-6">
          <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
            <div>
              <h2 className="text-xl font-semibold">People ({peopleTotalCount.toLocaleString()})</h2>
              <p className="text-sm text-muted-foreground">Search people by name, title, headline, or role description within this company.</p>
            </div>
            <div className="flex w-full gap-2 md:w-auto">
              <Input value={peopleQuery} onChange={(event) => setPeopleQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") loadCompanyDetail(selectedCompany, 0); }} placeholder="Search people..." className="w-full md:w-[300px]" />
              <Button variant="outline" onClick={() => loadCompanyDetail(selectedCompany, 0)} disabled={isLoadingPeople}>Search</Button>
            </div>
          </div>

          {isLoadingPeople ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> Loading people...</div>
          ) : people.length > 0 ? (
            <div className="space-y-3">
              <div className="text-sm text-muted-foreground">
                Showing {people.length ? peopleStart + 1 : 0}–{peopleStart + people.length} of {peopleTotalCount.toLocaleString()}
                {peopleQuery && <span> matching “{peopleQuery}”</span>}
              </div>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-8" />
                    <TableHead className="cursor-pointer select-none" onClick={() => handlePeopleSort("name")}>Name<SortIcon field="name" /></TableHead>
                    <TableHead>Position</TableHead>
                    <TableHead className="cursor-pointer select-none" onClick={() => handlePeopleSort("tenure")}>Tenure<SortIcon field="tenure" /></TableHead>
                    <TableHead className="cursor-pointer select-none" onClick={() => handlePeopleSort("current")}>Status<SortIcon field="current" /></TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {people.map((person, index) => {
                    const hasMultiplePositions = (person.positions_count || 0) > 1 && (person.all_positions || []).length > 0;
                    const isExpanded = expandedPersonIds.has(person.id);
                    return (
                      <React.Fragment key={`${person.id}-${index}`}>
                        <TableRow className={hasMultiplePositions ? "cursor-pointer" : ""} onClick={() => hasMultiplePositions && setExpandedPersonIds((prev) => { const next = new Set(prev); next.has(person.id) ? next.delete(person.id) : next.add(person.id); return next; })}>
                          <TableCell className="p-2">{hasMultiplePositions && (isExpanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />)}</TableCell>
                          <TableCell className="font-medium">
                            <span className="inline-flex items-center gap-2">
                              {person.name}
                              {person.public_identifier && !person.public_identifier.startsWith("synth-") && <SocialLinks linkedinUrl={`https://linkedin.com/in/${person.public_identifier}`} />}
                            </span>
                            {person.headline && <div className="mt-1 text-xs text-muted-foreground">{person.headline}</div>}
                          </TableCell>
                          <TableCell>
                            <div className="font-medium">{person.position_title || "—"}</div>
                            {person.position_description && <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">{person.position_description}</div>}
                            {hasMultiplePositions && <Badge variant="outline" className="mt-1 text-xs">+{(person.positions_count || 1) - 1} more</Badge>}
                          </TableCell>
                          <TableCell className="text-sm text-muted-foreground">{person.tenure_years ? `${person.tenure_years}y` : "-"}</TableCell>
                          <TableCell><Badge variant={person.is_current ? "default" : "secondary"} className="text-xs">{person.is_current ? "Current" : "Past"}</Badge></TableCell>
                        </TableRow>
                        {isExpanded && (person.all_positions || []).map((position, positionIndex) => (
                          <TableRow key={`${person.id}-position-${positionIndex}`} className="bg-muted/30">
                            <TableCell />
                            <TableCell className="pl-8 text-sm text-muted-foreground">↳</TableCell>
                            <TableCell>{position.position_title || "—"}{position.position_description && <div className="text-xs text-muted-foreground">{position.position_description}</div>}</TableCell>
                            <TableCell className="text-sm text-muted-foreground">{position.years ? `${position.years}y` : "-"}</TableCell>
                            <TableCell><Badge variant={position.is_current ? "default" : "secondary"} className="text-xs">{position.is_current ? "Current" : "Past"}</Badge></TableCell>
                          </TableRow>
                        ))}
                      </React.Fragment>
                    );
                  })}
                </TableBody>
              </Table>
              {peopleTotalPages > 1 && (
                <div className="flex items-center justify-center gap-4 border-t pt-4">
                  <Button variant="outline" size="sm" onClick={() => loadCompanyDetail(selectedCompany, Math.max(0, peoplePage - 1))} disabled={!peopleHasPrev || isLoadingPeople}><ChevronLeft className="mr-1 h-4 w-4" />Previous</Button>
                  <span className="text-sm text-muted-foreground">Page {peoplePage + 1} of {peopleTotalPages}</span>
                  <Button variant="outline" size="sm" onClick={() => loadCompanyDetail(selectedCompany, peoplePage + 1)} disabled={!peopleHasNext || isLoadingPeople}>Next<ChevronRight className="ml-1 h-4 w-4" /></Button>
                </div>
              )}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No people found for this company.</p>
          )}
        </Card>
      </div>
    );
  }

  return (
    <div className="container mx-auto max-w-6xl space-y-6 overflow-auto px-4 py-8">
      <div>
        <h1 className="text-2xl font-bold">Company Directory</h1>
        <p className="text-muted-foreground">Search local company data by name, sector type, or entity type.</p>
      </div>

      <Card className="space-y-4 p-6">
        <div className="grid gap-4 md:grid-cols-3">
          <div className="space-y-2">
            <label className="text-sm font-medium">Company name</label>
            <Input value={name} onChange={(event) => setName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") loadCompanies(0); }} placeholder="e.g. Stripe, OpenAI..." />
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium">Sector types</label>
            <Input value={sectorText} onChange={(event) => setSectorText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") loadCompanies(0); }} placeholder="Comma-separated sectors" list="local-company-sectors" />
            <datalist id="local-company-sectors">{sectorOptions.map((sector) => <option key={sector} value={sector} />)}</datalist>
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium">Entity types</label>
            <Input value={entityText} onChange={(event) => setEntityText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") loadCompanies(0); }} placeholder="Comma-separated entity types" list="local-company-entities" />
            <datalist id="local-company-entities">{entityOptions.map((entity) => <option key={entity} value={entity} />)}</datalist>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={() => loadCompanies(0)} disabled={isSearching} size="lg">
            {isSearching ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Search className="mr-2 h-4 w-4" />}
            Search
          </Button>
          <Button variant="outline" onClick={() => { setName(""); setSectorText(""); setEntityText(""); setTimeout(() => loadCompanies(0), 0); }} disabled={isSearching}>Clear filters</Button>
          {source && <span className="text-sm text-muted-foreground">Source: {source}</span>}
        </div>
      </Card>

      {error && <Card className="border-destructive/40 p-4 text-sm text-destructive">{error}</Card>}
      {warnings.length > 0 && <Card className="p-4 text-sm text-muted-foreground">{warnings.map((warning) => <div key={warning}>{warning}</div>)}</Card>}

      {isSearching ? (
        <Card className="p-6"><div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> Searching companies...</div></Card>
      ) : companies.length > 0 ? (
        <Card className="p-6">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-semibold">Results ({offset + 1}-{Math.min(offset + companies.length, totalCount)} of {totalCount.toLocaleString()})</h2>
              <div className="text-sm text-muted-foreground">Page {currentPage} of {totalPages}</div>
            </div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[60px]">#</TableHead>
                  <TableHead>Company</TableHead>
                  <TableHead>Sectors</TableHead>
                  <TableHead>Entity Type</TableHead>
                  <TableHead>Headcount</TableHead>
                  <TableHead>People</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {companies.map((company, index) => (
                  <TableRow key={company.id} className="cursor-pointer" onClick={() => { setPeopleQuery(""); loadCompanyDetail(company, 0); }}>
                    <TableCell className="font-medium">{offset + index + 1}</TableCell>
                    <TableCell>
                      <div className="font-semibold">{company.name}</div>
                      {locationText(company) && <div className="text-xs text-muted-foreground">{locationText(company)}</div>}
                    </TableCell>
                    <TableCell><div className="flex flex-wrap gap-1">{(company.sector_types || []).slice(0, 3).map((sector) => <Badge key={sector} variant="secondary" className="text-xs">{sector}</Badge>)}{(company.sector_types || []).length > 3 && <span className="text-xs text-muted-foreground">+{(company.sector_types || []).length - 3} more</span>}</div></TableCell>
                    <TableCell>{company.entity_types?.[0] && <Badge variant="outline" className="text-xs">{company.entity_types[0]}</Badge>}</TableCell>
                    <TableCell>{company.headcount?.toLocaleString() ?? "-"}</TableCell>
                    <TableCell>{company.people_count?.toLocaleString() ?? "-"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {totalPages > 1 && (
              <div className="flex items-center justify-center gap-4 border-t pt-4">
                <Button variant="outline" size="sm" onClick={() => loadCompanies(Math.max(0, offset - PAGE_SIZE))} disabled={offset <= 0 || isSearching}><ChevronLeft className="mr-1 h-4 w-4" />Previous</Button>
                <span className="text-sm text-muted-foreground">Page {currentPage} of {totalPages}</span>
                <Button variant="outline" size="sm" onClick={() => loadCompanies(offset + PAGE_SIZE)} disabled={offset + PAGE_SIZE >= totalCount || isSearching}>Next<ChevronRight className="ml-1 h-4 w-4" /></Button>
              </div>
            )}
          </div>
        </Card>
      ) : (
        <Card className="p-6 text-sm text-muted-foreground">
          No companies found. The local API checks POWERPACKS_LOCAL_SEARCH_DB, .powerpacks/search-index, run artifact dirs, and .powerpacks/network-import/merged/people.csv.
        </Card>
      )}
    </div>
  );
}
