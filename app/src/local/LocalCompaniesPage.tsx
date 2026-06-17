import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown, Building2, Info, Search } from "lucide-react";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  fetchLocalCompanies,
  sectorLabel,
  LocalCompaniesResponse,
  LocalCompanyRow,
} from "./companiesApi";

// ============================================================================
// Constants & Types
// ============================================================================

const PAGE_SIZE = 50;

type SortField = "current_people" | "total_people" | "company_name" | "headcount" | "founded_year";
type SortDir = "asc" | "desc";

const VALID_SORT_FIELDS: SortField[] = [
  "current_people",
  "total_people",
  "company_name",
  "headcount",
  "founded_year",
];

// Default sort is people-in-network descending (most relevant companies first).
const DEFAULT_SORT: SortField = "current_people";

function defaultDirFor(field: SortField): SortDir {
  return field === "company_name" ? "asc" : "desc";
}

interface UrlState {
  q: string;
  sort: SortField;
  dir: SortDir;
  page: number;
}

function readUrlState(): UrlState {
  const params = new URLSearchParams(window.location.search);
  const rawSort = params.get("sort") as SortField | null;
  const sort = rawSort && VALID_SORT_FIELDS.includes(rawSort) ? rawSort : DEFAULT_SORT;
  const rawDir = params.get("dir");
  return {
    q: params.get("q") || "",
    sort,
    dir: rawDir === "desc" || rawDir === "asc" ? rawDir : defaultDirFor(sort),
    page: parseInt(params.get("page") || "0", 10) || 0,
  };
}

function navigateLocal(path: string) {
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

// ============================================================================
// Component
// ============================================================================

export function LocalCompaniesPage() {
  // ── URL-derived state (single source of truth — survives back-navigation) ──
  const [urlState, setUrlState] = useState<UrlState>(() => readUrlState());
  const { q: debouncedSearch, sort: sortField, dir: sortDir, page } = urlState;
  // Local state only for things that need debouncing
  const [search, setSearch] = useState(debouncedSearch);

  const [result, setResult] = useState<LocalCompaniesResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isFetching, setIsFetching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ── URL param helpers (same pattern as LocalContactsPage) ──
  const updateParams = useCallback((updates: Record<string, string | null>) => {
    const params = new URLSearchParams(window.location.search);
    for (const [key, val] of Object.entries(updates)) {
      if (val) params.set(key, val);
      else params.delete(key);
    }
    const query = params.toString();
    window.history.replaceState({}, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
    setUrlState(readUrlState());
  }, []);

  const setPage = useCallback(
    (newPage: number) => {
      updateParams({ page: newPage > 0 ? String(newPage) : null });
    },
    [updateParams]
  );

  // Resync on browser back/forward
  useEffect(() => {
    const handlePopState = () => {
      const next = readUrlState();
      setUrlState(next);
      setSearch(next.q);
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  // Debounce search → sync to URL (skip initial mount to preserve page on back-nav)
  const searchMountedRef = useRef(false);
  useEffect(() => {
    if (!searchMountedRef.current) {
      searchMountedRef.current = true;
      return;
    }
    const timer = setTimeout(() => {
      updateParams({ q: search || null, page: null });
    }, 300);
    return () => clearTimeout(timer);
  }, [search]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Fetch companies (keep previous page while fetching) ──
  useEffect(() => {
    let cancelled = false;
    setIsFetching(true);
    setError(null);
    fetchLocalCompanies({ q: debouncedSearch || undefined, sort: sortField, dir: sortDir, page, pageSize: PAGE_SIZE })
      .then((response) => {
        if (cancelled) return;
        setResult(response);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load companies");
      })
      .finally(() => {
        if (cancelled) return;
        setIsFetching(false);
        setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [debouncedSearch, sortField, sortDir, page]);

  const companies = result?.rows || [];
  const totalCount = result?.total || 0;
  const totalPages = Math.ceil(totalCount / PAGE_SIZE);
  const indexMissing = !!result?.index_missing;
  const columnCount = 6;

  // Sort toggle — same semantics as LocalContactsPage's handleSort
  const handleSort = useCallback(
    (field: SortField) => {
      if (sortField === field) {
        const next: SortDir = sortDir === "asc" ? "desc" : "asc";
        updateParams({ dir: next === defaultDirFor(field) ? null : next, page: null });
      } else {
        updateParams({ sort: field === DEFAULT_SORT ? null : field, dir: null, page: null });
      }
    },
    [sortField, sortDir, updateParams]
  );

  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return <ArrowUpDown className="h-4 w-4 ml-1 opacity-40" />;
    return sortDir === "asc" ? <ArrowUp className="h-4 w-4 ml-1" /> : <ArrowDown className="h-4 w-4 ml-1" />;
  };

  // ── Skeleton rows ──
  const renderSkeletons = () =>
    Array.from({ length: 10 }).map((_, i) => (
      <TableRow key={i} className="h-10">
        <TableCell className="py-1.5">
          <div className="flex items-center gap-2">
            <div className="h-6 w-6 shrink-0 animate-pulse rounded-md bg-muted" />
            <div className="h-4 w-24 animate-pulse rounded-md bg-muted" />
          </div>
        </TableCell>
        <TableCell className="py-1.5 hidden md:table-cell"><div className="h-4 w-40 animate-pulse rounded-md bg-muted" /></TableCell>
        <TableCell className="py-1.5 hidden lg:table-cell"><div className="h-4 w-28 animate-pulse rounded-md bg-muted" /></TableCell>
        <TableCell className="py-1.5 hidden lg:table-cell"><div className="h-4 w-24 animate-pulse rounded-md bg-muted" /></TableCell>
        <TableCell className="py-1.5 hidden lg:table-cell"><div className="ml-auto h-4 w-12 animate-pulse rounded-md bg-muted" /></TableCell>
        <TableCell className="py-1.5"><div className="mx-auto h-4 w-8 animate-pulse rounded-md bg-muted" /></TableCell>
      </TableRow>
    ));

  // ── Pagination ──
  const renderPagination = () => {
    if (totalCount <= 0) return null;
    return (
      <div className="flex items-center justify-between px-4 py-3 border-t text-xs text-muted-foreground">
        <span>
          Showing {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, totalCount)} of{" "}
          {totalCount.toLocaleString()}
        </span>
        <div className="flex gap-1">
          <Button variant="ghost" size="sm" className="h-7 text-xs" disabled={page === 0 || isFetching} onClick={() => setPage(page - 1)}>
            ← Prev
          </Button>
          <Button variant="ghost" size="sm" className="h-7 text-xs" disabled={page >= totalPages - 1 || isFetching} onClick={() => setPage(page + 1)}>
            Next →
          </Button>
        </div>
      </div>
    );
  };

  return (
    <div className="overflow-x-hidden">
      <div className="mb-6">
        <h1 className="text-2xl font-bold">Companies</h1>
        <p className="text-muted-foreground">Browse companies and the people who work there</p>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-col gap-2">
            <div className="relative w-full sm:w-[280px]">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
              <Input
                placeholder="Search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="h-8 pl-8 pr-7 text-xs"
              />
              <Tooltip>
                <TooltipTrigger asChild>
                  <Info className="absolute right-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground/40 hover:text-muted-foreground cursor-help transition-colors" />
                </TooltipTrigger>
                <TooltipContent side="bottom" className="text-xs space-y-1 max-w-[220px]">
                  <p className="font-medium">Search prefixes</p>
                  <p className="text-muted-foreground">Default (no prefix) searches name, aliases, and domain</p>
                  <p><code className="text-[11px]">sector:</code> sector (e.g. <code className="text-[11px]">sector:fintech</code>)</p>
                  <p><code className="text-[11px]">city:</code> HQ city (e.g. <code className="text-[11px]">city:san mateo</code>)</p>
                </TooltipContent>
              </Tooltip>
            </div>
            {!isLoading && !indexMissing && (
              <p className="pl-0.5 text-xs text-muted-foreground tabular-nums">
                <span className="font-semibold text-foreground">{totalCount.toLocaleString()}</span>{" "}
                {debouncedSearch
                  ? `matching ${totalCount === 1 ? "company" : "companies"}`
                  : "companies"}
              </p>
            )}
          </div>
        </CardHeader>

        <CardContent className="p-0">
          {error && (
            <div className="px-4 py-3 text-sm text-destructive border-b">{error}</div>
          )}

          {indexMissing ? (
            <div className="px-6 py-12 text-center text-sm text-muted-foreground">
              <p className="font-medium text-foreground">Local search index not found</p>
              <p className="mt-1">
                Run{" "}
                <a
                  href="/sources/linkedin"
                  className="text-primary hover:underline"
                  onClick={(e) => {
                    e.preventDefault();
                    navigateLocal("/sources/linkedin");
                  }}
                >
                  import a source
                </a>{" "}
                to build <code className="text-xs">.powerpacks/search-index/local-search.duckdb</code> first.
              </p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow className="text-xs">
                    <TableHead className="w-[200px] cursor-pointer select-none whitespace-nowrap" onClick={() => handleSort("company_name")}>
                      <span className="flex items-center">Company <SortIcon field="company_name" /></span>
                    </TableHead>
                    <TableHead className="whitespace-nowrap hidden md:table-cell">Description</TableHead>
                    <TableHead className="w-[160px] whitespace-nowrap hidden lg:table-cell">Location</TableHead>
                    <TableHead className="whitespace-nowrap hidden lg:table-cell">Sector</TableHead>
                    <TableHead className="w-[110px] cursor-pointer select-none whitespace-nowrap hidden lg:table-cell" onClick={() => handleSort("headcount")}>
                      <span className="flex items-center justify-end">Headcount <SortIcon field="headcount" /></span>
                    </TableHead>
                    <TableHead className="w-[70px] text-center cursor-pointer select-none" onClick={() => handleSort("current_people")}>
                      <span className="flex items-center justify-center">People <SortIcon field="current_people" /></span>
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {isLoading || isFetching ? (
                    renderSkeletons()
                  ) : companies.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={columnCount} className="text-center text-muted-foreground py-12 text-sm">
                        {debouncedSearch ? `No companies matching "${debouncedSearch}"` : "No companies found"}
                      </TableCell>
                    </TableRow>
                  ) : (
                    companies.map((company) => <CompanyRow key={company.id} company={company} />)
                  )}
                </TableBody>
              </Table>
            </div>
          )}

          {!indexMissing && renderPagination()}
        </CardContent>
      </Card>
    </div>
  );
}

// ============================================================================
// Company Row
// ============================================================================

const CompanyRow = ({ company }: { company: LocalCompanyRow }) => {
  const companyHref = `/companies/${encodeURIComponent(company.id)}`;
  const locationText = [company.city, company.state].filter(Boolean).join(", ") || company.metro_area || "";
  const sectors = company.sector_types.slice(0, 2);
  const hiddenSectorCount = Math.max(0, company.sector_types.length - sectors.length);
  const domainHref = company.website_domain
    ? company.website_domain.startsWith("http")
      ? company.website_domain
      : `https://${company.website_domain}`
    : null;

  const openCompany = () => navigateLocal(companyHref);

  return (
    <TableRow className="cursor-pointer hover:bg-muted/50 text-xs h-10" onClick={openCompany}>
      <TableCell className="py-1.5">
        <div className="flex items-center gap-2 min-w-0">
          <Avatar className="h-6 w-6 shrink-0 rounded-md">
            <AvatarImage src={company.logo_url || undefined} className="object-contain" />
            <AvatarFallback className="rounded-md text-[10px]">
              <Building2 className="h-3.5 w-3.5 text-muted-foreground" />
            </AvatarFallback>
          </Avatar>
          <a
            href={companyHref}
            className="truncate font-medium max-w-[160px] block hover:text-primary hover:underline"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              openCompany();
            }}
          >
            {company.company_name || "Unknown"}
          </a>
        </div>
      </TableCell>
      <TableCell className="py-1.5 hidden md:table-cell">
        <div className="max-w-[400px] min-w-0">
          {company.description ? (
            <span className="text-muted-foreground truncate block">{company.description}</span>
          ) : (
            <span className="text-muted-foreground/40">—</span>
          )}
          {domainHref && (
            <a
              href={domainHref}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[11px] text-muted-foreground/70 hover:text-primary hover:underline truncate block"
              onClick={(e) => e.stopPropagation()}
            >
              {company.website_domain}
            </a>
          )}
        </div>
      </TableCell>
      <TableCell className="py-1.5 hidden lg:table-cell">
        {locationText ? (
          <span className="text-muted-foreground truncate block max-w-[160px]">{locationText}</span>
        ) : (
          <span className="text-muted-foreground/40">—</span>
        )}
      </TableCell>
      <TableCell className="py-1.5 hidden lg:table-cell">
        {sectors.length === 0 ? (
          <span className="text-muted-foreground/40">—</span>
        ) : (
          <div className="flex flex-wrap items-center gap-1">
            {sectors.map((sector) => (
              <Badge key={sector} variant="secondary" className="max-w-[140px] text-[10px] font-normal">
                <span className="truncate">{sectorLabel(sector)}</span>
              </Badge>
            ))}
            {hiddenSectorCount > 0 && (
              <span
                className="text-[10px] text-muted-foreground cursor-default"
                title={company.sector_types.slice(2).map(sectorLabel).join(", ")}
              >
                +{hiddenSectorCount}
              </span>
            )}
          </div>
        )}
      </TableCell>
      <TableCell className="py-1.5 hidden lg:table-cell text-right tabular-nums">
        {company.headcount ? (
          <span className="text-muted-foreground">{company.headcount.toLocaleString()}</span>
        ) : (
          <span className="text-muted-foreground/40">—</span>
        )}
      </TableCell>
      <TableCell className="py-1.5 text-center tabular-nums">
        {company.current_people ? (
          <span>{company.current_people.toLocaleString()}</span>
        ) : (
          <span className="text-muted-foreground/30">—</span>
        )}
      </TableCell>
    </TableRow>
  );
};
