import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown, Info, Mail, MessageCircle, Search } from "lucide-react";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { LinkedinLink, XLink } from "@/components/ui/social-link";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { fetchLocalContacts, LocalContactRow, LocalContactsResponse } from "./contactsApi";

// ============================================================================
// Constants & Types
// ============================================================================

const PAGE_SIZE = 50;

type SortField = "total_interactions" | "first_name" | "last_name" | "headline" | "current_company" | "city";
type SortDir = "asc" | "desc";

const VALID_SORT_FIELDS: SortField[] = [
  "total_interactions",
  "first_name",
  "last_name",
  "headline",
  "current_company",
  "city",
];

// Mirrors prod ContactsV2: default sort is total interactions descending.
const DEFAULT_SORT: SortField = "total_interactions";

function defaultDirFor(field: SortField): SortDir {
  return field === "total_interactions" ? "desc" : "asc";
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

const getInitials = (contact: LocalContactRow) => {
  const first = contact.first_name?.[0] || contact.full_name?.[0] || "";
  const last = contact.last_name?.[0] || "";
  return (first + last).toUpperCase() || "?";
};

const getDisplayName = (contact: LocalContactRow) => {
  if (contact.full_name?.trim()) return contact.full_name.trim();
  const parts = [contact.first_name, contact.last_name].filter(Boolean);
  return parts.join(" ") || "Unknown";
};

// ============================================================================
// Component
// ============================================================================

export function LocalContactsPage() {
  // ── URL-derived state (single source of truth — survives back-navigation) ──
  const [urlState, setUrlState] = useState<UrlState>(() => readUrlState());
  const { q: debouncedSearch, sort: sortField, dir: sortDir, page } = urlState;
  // Local state only for things that need debouncing
  const [search, setSearch] = useState(debouncedSearch);

  const [result, setResult] = useState<LocalContactsResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isFetching, setIsFetching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ── URL param helpers (same pattern as ContactsV2) ──
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

  // ── Fetch contacts (placeholder-keep previous page while fetching) ──
  useEffect(() => {
    let cancelled = false;
    setIsFetching(true);
    setError(null);
    fetchLocalContacts({ q: debouncedSearch || undefined, sort: sortField, dir: sortDir, page, pageSize: PAGE_SIZE })
      .then((response) => {
        if (cancelled) return;
        setResult(response);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load contacts");
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

  const contacts = result?.rows || [];
  const totalCount = result?.total || 0;
  const totalPages = Math.ceil(totalCount / PAGE_SIZE);
  const indexMissing = !!result?.index_missing;
  // Only hide the column once the backend has told us msgvault is unavailable.
  const showInteractions = result?.interactions_available !== false;
  const columnCount = showInteractions ? 5 : 4;

  // Sort toggle — same semantics as ContactsV2's handleSort
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
            <div className="h-6 w-6 shrink-0 animate-pulse rounded-full bg-muted" />
            <div className="h-4 w-20 animate-pulse rounded-md bg-muted" />
          </div>
        </TableCell>
        <TableCell className="py-1.5 hidden md:table-cell"><div className="h-4 w-32 animate-pulse rounded-md bg-muted" /></TableCell>
        <TableCell className="py-1.5 hidden lg:table-cell"><div className="h-4 w-20 animate-pulse rounded-md bg-muted" /></TableCell>
        <TableCell className="py-1.5"><div className="h-4 w-8 animate-pulse rounded-md bg-muted" /></TableCell>
        {showInteractions && (
          <TableCell className="py-1.5"><div className="mx-auto h-4 w-8 animate-pulse rounded-md bg-muted" /></TableCell>
        )}
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
        <h1 className="text-2xl font-bold">Contacts</h1>
        <p className="text-muted-foreground">Browse and manage your enriched contacts</p>
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
                  <p className="text-muted-foreground">Default (no prefix) searches by name</p>
                  <p><code className="text-[11px]">headline:</code> title/role</p>
                  <p><code className="text-[11px]">email:</code> email address</p>
                  <p><code className="text-[11px]">company:</code> company name</p>
                  <p><code className="text-[11px]">phone:</code> any phone or partial digits (e.g. <code className="text-[11px]">phone:408</code>)</p>
                  <p><code className="text-[11px]">twitter:</code> any X handle or partial handle</p>
                  <p><code className="text-[11px]">city:</code> city/location</p>
                </TooltipContent>
              </Tooltip>
            </div>
            {!isLoading && !indexMissing && (
              <p className="pl-0.5 text-xs text-muted-foreground tabular-nums">
                <span className="font-semibold text-foreground">{totalCount.toLocaleString()}</span>{" "}
                {debouncedSearch
                  ? `matching ${totalCount === 1 ? "contact" : "contacts"}`
                  : "contacts"}
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
                    <TableHead className="w-[140px] cursor-pointer select-none whitespace-nowrap" onClick={() => handleSort("first_name")}>
                      <span className="flex items-center">Name <SortIcon field="first_name" /></span>
                    </TableHead>
                    <TableHead className="cursor-pointer select-none whitespace-nowrap hidden md:table-cell" onClick={() => handleSort("headline")}>
                      <span className="flex items-center">Headline <SortIcon field="headline" /></span>
                    </TableHead>
                    <TableHead className="w-[160px] cursor-pointer select-none whitespace-nowrap hidden lg:table-cell" onClick={() => handleSort("city")}>
                      <span className="flex items-center">Location <SortIcon field="city" /></span>
                    </TableHead>
                    <TableHead className="w-[80px]">Social</TableHead>
                    {showInteractions && (
                      <TableHead className="w-[60px] text-center cursor-pointer select-none" onClick={() => handleSort("total_interactions")}>
                        <span className="flex items-center justify-center">Msgs <SortIcon field="total_interactions" /></span>
                      </TableHead>
                    )}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {isLoading || isFetching ? (
                    renderSkeletons()
                  ) : contacts.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={columnCount} className="text-center text-muted-foreground py-12 text-sm">
                        {debouncedSearch ? `No contacts matching "${debouncedSearch}"` : "No contacts found"}
                      </TableCell>
                    </TableRow>
                  ) : (
                    contacts.map((contact) => (
                      <ContactRow key={contact.person_id} contact={contact} showInteractions={showInteractions} />
                    ))
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
// Contact Row
// ============================================================================

const ContactRow = ({ contact, showInteractions }: { contact: LocalContactRow; showInteractions: boolean }) => {
  const profileHref = `/contacts/${encodeURIComponent(contact.person_id)}`;
  const linkedinUrl = contact.linkedin_url?.trim() || contact.public_profile_url?.trim() ||
    (contact.public_identifier ? `https://linkedin.com/in/${contact.public_identifier}` : null);
  const xHandle = contact.x_twitter_handle?.trim() || contact.twitter_handle?.trim() || "";
  const headline = contact.headline || contact.current_title || null;
  const location =
    [contact.city, contact.state].filter(Boolean).join(", ") || contact.location_raw || null;

  const allEmails = (contact.all_emails || []).filter(Boolean);
  const primaryEmail = contact.primary_email?.trim() || null;
  const emailList = allEmails.length > 0 ? allEmails : primaryEmail ? [primaryEmail] : [];
  const allPhones = (contact.all_phones || []).filter(Boolean);
  const primaryPhone = contact.primary_phone?.trim() || null;
  const phoneList = allPhones.length > 0 ? allPhones : primaryPhone ? [primaryPhone] : [];

  const openProfile = () => navigateLocal(profileHref);

  return (
    <TableRow className="cursor-pointer hover:bg-muted/50 text-xs h-10" onClick={openProfile}>
      <TableCell className="py-1.5">
        <div className="flex items-center gap-2 min-w-0">
          <Avatar className="h-6 w-6 shrink-0">
            <AvatarImage src={contact.profile_picture_url || undefined} />
            <AvatarFallback className="text-[10px]">{getInitials(contact)}</AvatarFallback>
          </Avatar>
          <a
            href={profileHref}
            className="truncate font-medium max-w-[120px] block hover:text-primary hover:underline"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              openProfile();
            }}
          >
            {getDisplayName(contact)}
          </a>
        </div>
      </TableCell>
      <TableCell className="py-1.5 hidden md:table-cell">
        <div className="max-w-[400px]">
          {headline ? (
            <span className="text-muted-foreground truncate block">{headline}</span>
          ) : (
            <span className="text-muted-foreground/40">—</span>
          )}
        </div>
      </TableCell>
      <TableCell className="py-1.5 hidden lg:table-cell">
        {location ? (
          <span className="text-muted-foreground truncate block max-w-[160px]">{location}</span>
        ) : (
          <span className="text-muted-foreground/40">—</span>
        )}
      </TableCell>
      <TableCell className="py-1.5">
        {!linkedinUrl && !xHandle && phoneList.length === 0 && emailList.length === 0 ? (
          <span className="text-muted-foreground/30">—</span>
        ) : (
          <div className="inline-flex items-center gap-1.5">
            {linkedinUrl && <LinkedinLink href={linkedinUrl} />}
            {xHandle && <XLink href={`https://x.com/${xHandle.replace(/^@/, "")}`} size={12} />}
            {emailList.length > 0 && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span
                    className="inline-flex items-center text-muted-foreground/70 hover:text-foreground transition-colors cursor-default"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <Mail className="h-3.5 w-3.5" />
                  </span>
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-xs text-xs">
                  <div className="space-y-0.5">
                    {emailList.map((email) => (
                      <p key={email}>{email}</p>
                    ))}
                  </div>
                </TooltipContent>
              </Tooltip>
            )}
            {phoneList.length > 0 && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span
                    className="inline-flex items-center text-muted-foreground/70 hover:text-foreground transition-colors cursor-default"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <MessageCircle className="h-3.5 w-3.5" />
                  </span>
                </TooltipTrigger>
                <TooltipContent side="top" className="max-w-xs text-xs">
                  <div className="space-y-0.5">
                    {phoneList.map((phone) => (
                      <p key={phone}>{phone}</p>
                    ))}
                  </div>
                </TooltipContent>
              </Tooltip>
            )}
          </div>
        )}
      </TableCell>
      {showInteractions && (
        <TableCell className="py-1.5 text-center tabular-nums">
          {contact.total_interactions ? (
            <span>{contact.total_interactions.toLocaleString()}</span>
          ) : (
            <span className="text-muted-foreground/30">—</span>
          )}
        </TableCell>
      )}
    </TableRow>
  );
};
