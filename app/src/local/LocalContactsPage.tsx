import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown, Info, Loader2, Mail, MessageCircle, Search } from "lucide-react";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { LinkedinLink, XLink } from "@/components/ui/social-link";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { fetchContacts } from "./powerpacksApi";
import type { ContactsSortDir, ContactsSortField, ContactsV2Result, UnifiedContact } from "./types";

const PAGE_SIZE = 50;
const SORT_FIELDS: ContactsSortField[] = ["first_name", "last_name", "headline", "location_raw", "total_interactions"];

function getDisplayName(contact: UnifiedContact): string {
  const display = contact.display_name?.trim();
  if (display) return display;
  const name = [contact.first_name, contact.last_name].filter(Boolean).join(" ").trim();
  return name || contact.primary_email || "Unknown";
}

function getInitials(contact: UnifiedContact): string {
  const first = contact.first_name?.[0] || contact.display_name?.trim()?.[0] || "";
  const last = contact.last_name?.[0] || "";
  return (first + last).toUpperCase() || "?";
}

function normalizeLinkedinUrl(contact: UnifiedContact): string | null {
  const direct = contact.linkedin_url || contact.confirmed_linkedin_url || contact.public_profile_url || contact.llm_selected_linkedin || contact.pass1_linkedin_url;
  if (direct) return direct;
  if (contact.public_identifier && !contact.public_identifier.startsWith("synth-")) return `https://linkedin.com/in/${contact.public_identifier}`;
  return null;
}

function normalizeXUrl(contact: UnifiedContact): string | null {
  const direct = contact.x_url;
  if (direct) return direct;
  const handle = contact.x_twitter_handle?.trim();
  if (!handle) return null;
  return handle.startsWith("http") ? handle : `https://x.com/${handle.replace(/^@/, "")}`;
}

function emailsFor(contact: UnifiedContact): string[] {
  const emails = contact.all_emails?.length ? contact.all_emails : contact.emails || [];
  const withPrimary = contact.primary_email ? [contact.primary_email, ...emails] : emails;
  return Array.from(new Set(withPrimary.map((email) => email.trim()).filter((email) => email && !email.endsWith("@linkedin.invalid"))));
}

function phonesFor(contact: UnifiedContact): string[] {
  const phones = contact.phone_numbers?.length ? contact.phone_numbers : contact.phone_number ? [contact.phone_number] : [];
  return Array.from(new Set(phones.map((phone) => phone.trim()).filter(Boolean)));
}

function SortIcon({ active, dir }: { active: boolean; dir: ContactsSortDir }) {
  if (!active) return <ArrowUpDown className="ml-1 h-3 w-3 opacity-40" />;
  return dir === "asc" ? <ArrowUp className="ml-1 h-3 w-3" /> : <ArrowDown className="ml-1 h-3 w-3" />;
}

function ContactRow({ contact }: { contact: UnifiedContact }) {
  const name = getDisplayName(contact);
  const headline = contact.headline || contact.current_title || "";
  const linkedinUrl = normalizeLinkedinUrl(contact);
  const xUrl = normalizeXUrl(contact);
  const emails = emailsFor(contact);
  const phones = phonesFor(contact);
  const messages = Number(contact.total_messages ?? contact.total_interactions ?? 0);

  return (
    <TableRow className="h-10 text-xs">
      <TableCell className="py-1.5">
        <div className="flex min-w-0 items-center gap-2">
          <Avatar className="h-6 w-6 shrink-0">
            <AvatarImage src={contact.profile_picture_url || undefined} />
            <AvatarFallback className="text-[10px]">{getInitials(contact)}</AvatarFallback>
          </Avatar>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="block max-w-[160px] truncate font-medium">{name}</span>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs">{name}</TooltipContent>
          </Tooltip>
        </div>
      </TableCell>
      <TableCell className="hidden py-1.5 md:table-cell">
        {headline ? (
          <Tooltip>
            <TooltipTrigger asChild><span className="block max-w-[420px] truncate text-muted-foreground">{headline}</span></TooltipTrigger>
            <TooltipContent side="top" className="max-w-xs text-xs">{headline}</TooltipContent>
          </Tooltip>
        ) : <span className="text-muted-foreground/40">—</span>}
      </TableCell>
      <TableCell className="hidden py-1.5 lg:table-cell">
        {contact.location_raw ? (
          <Tooltip>
            <TooltipTrigger asChild><span className="block max-w-[170px] truncate text-muted-foreground">{contact.location_raw}</span></TooltipTrigger>
            <TooltipContent side="top" className="text-xs">{contact.location_raw}</TooltipContent>
          </Tooltip>
        ) : <span className="text-muted-foreground/40">—</span>}
      </TableCell>
      <TableCell className="py-1.5">
        {!linkedinUrl && !xUrl && phones.length === 0 ? <span className="text-muted-foreground/30">—</span> : (
          <div className="inline-flex items-center gap-1.5">
            {linkedinUrl && <LinkedinLink href={linkedinUrl} size={13} />}
            {xUrl && <XLink href={xUrl} size={12} />}
            {phones.length > 0 && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="inline-flex text-muted-foreground/70"><MessageCircle className="h-3.5 w-3.5" /></span>
                </TooltipTrigger>
                <TooltipContent side="top" className="text-xs"><div className="flex flex-col gap-0.5">{phones.map((p) => <span key={p}>{p}</span>)}</div></TooltipContent>
              </Tooltip>
            )}
          </div>
        )}
      </TableCell>
      <TableCell className="py-1.5 text-center">
        {emails.length > 0 ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="inline-flex items-center gap-1 text-muted-foreground"><Mail className="h-3.5 w-3.5" />{emails.length > 1 && <span className="text-[9px] font-medium">{emails.length}</span>}</span>
            </TooltipTrigger>
            <TooltipContent side="top" className="text-xs"><div className="flex flex-col gap-0.5">{emails.map((email) => <span key={email}>{email}</span>)}</div></TooltipContent>
          </Tooltip>
        ) : <span className="text-muted-foreground/30">—</span>}
      </TableCell>
      <TableCell className="py-1.5 text-center">
        {messages > 0 ? <Badge variant="secondary" className="px-1.5 py-0 text-[10px]">{messages.toLocaleString()}</Badge> : <span className="text-muted-foreground/30">—</span>}
      </TableCell>
    </TableRow>
  );
}

export function LocalContactsPage() {
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [sortField, setSortField] = useState<ContactsSortField>("total_interactions");
  const [sortDir, setSortDir] = useState<ContactsSortDir>("desc");
  const [result, setResult] = useState<ContactsV2Result | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedSearch(search.trim());
      setPage(0);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchContacts({ page, pageSize: PAGE_SIZE, search: debouncedSearch, sortField, sortDir })
      .then((next) => { if (!cancelled) setResult(next); })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load contacts"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [page, debouncedSearch, sortField, sortDir]);

  const contacts = result?.data || [];
  const totalCount = result?.total_count || 0;
  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));

  const sortLabel = useMemo(() => ({
    first_name: "First name",
    last_name: "Last name",
    headline: "Headline",
    location_raw: "Location",
    total_interactions: "Messages",
  } satisfies Record<ContactsSortField, string>), []);

  const handleSort = useCallback((field: ContactsSortField) => {
    if (!SORT_FIELDS.includes(field)) return;
    setPage(0);
    if (sortField === field) setSortDir((current) => current === "asc" ? "desc" : "asc");
    else {
      setSortField(field);
      setSortDir(field === "total_interactions" ? "desc" : "asc");
    }
  }, [sortField]);

  const handleSortFieldChange = useCallback((field: ContactsSortField) => {
    if (!SORT_FIELDS.includes(field)) return;
    setPage(0);
    setSortField(field);
    setSortDir(field === "total_interactions" ? "desc" : "asc");
  }, []);

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-6">
      <div>
        <h1 className="text-2xl font-semibold">My Contacts</h1>
        <p className="mt-1 text-sm text-muted-foreground">Browse local contacts from your Powerpacks artifacts or local DuckDB.</p>
      </div>

      {error && <Card className="border-destructive/40 bg-destructive/5"><CardContent className="py-3 text-sm text-destructive">{error}</CardContent></Card>}

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2">
              <Button size="sm" className="h-7 text-xs">My Contacts</Button>
              {totalCount > 0 && <Badge variant="secondary" className="text-xs font-normal">{totalCount.toLocaleString()}</Badge>}
              {result?.source && <Badge variant="outline" className="text-xs font-normal">{result.source}</Badge>}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <select
                value={sortField}
                onChange={(event) => handleSortFieldChange(event.target.value as ContactsSortField)}
                className="h-8 rounded-md border bg-background px-2 text-xs"
                aria-label="Sort contacts"
              >
                {SORT_FIELDS.map((field) => <option key={field} value={field}>{sortLabel[field]}</option>)}
              </select>
              <Button variant="outline" size="sm" className="h-8 text-xs" onClick={() => setSortDir((d) => d === "asc" ? "desc" : "asc")}>
                {sortDir === "asc" ? "Ascending" : "Descending"}
              </Button>
              <div className="relative w-[240px]">
                <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input placeholder="Search name, email, phone..." value={search} onChange={(e) => setSearch(e.target.value)} className="h-8 pl-8 pr-7 text-xs" />
                <Tooltip>
                  <TooltipTrigger asChild><Info className="absolute right-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 cursor-help text-muted-foreground/50" /></TooltipTrigger>
                  <TooltipContent side="bottom" className="max-w-[240px] space-y-1 text-xs">
                    <p className="font-medium">Search prefixes</p>
                    <p>Default searches names.</p><p><code>headline:</code> title/role</p><p><code>email:</code> email address</p><p><code>phone:</code> phone number</p>
                  </TooltipContent>
                </Tooltip>
              </div>
            </div>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow className="text-xs">
                  <TableHead className="w-[180px] cursor-pointer select-none whitespace-nowrap" onClick={() => handleSort("first_name")}><span className="flex items-center">Name <SortIcon active={sortField === "first_name"} dir={sortDir} /></span></TableHead>
                  <TableHead className="hidden cursor-pointer select-none whitespace-nowrap md:table-cell" onClick={() => handleSort("headline")}><span className="flex items-center">Headline <SortIcon active={sortField === "headline"} dir={sortDir} /></span></TableHead>
                  <TableHead className="hidden w-[180px] cursor-pointer select-none whitespace-nowrap lg:table-cell" onClick={() => handleSort("location_raw")}><span className="flex items-center">Location <SortIcon active={sortField === "location_raw"} dir={sortDir} /></span></TableHead>
                  <TableHead className="w-[90px]">LinkedIn/X/Phone</TableHead>
                  <TableHead className="w-[60px] text-center">Email</TableHead>
                  <TableHead className="w-[70px] cursor-pointer select-none text-center" onClick={() => handleSort("total_interactions")}><span className="flex items-center justify-center">Msgs <SortIcon active={sortField === "total_interactions"} dir={sortDir} /></span></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {loading ? (
                  <TableRow><TableCell colSpan={6} className="py-12 text-center text-sm text-muted-foreground"><span className="inline-flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" /> Loading contacts...</span></TableCell></TableRow>
                ) : contacts.length === 0 ? (
                  <TableRow><TableCell colSpan={6} className="py-12 text-center text-sm text-muted-foreground">
                    {debouncedSearch ? `No contacts matching "${debouncedSearch}"` : "No contacts found. Checked local DuckDB and .powerpacks contact/profile artifacts."}
                    {result?.warnings?.length ? <div className="mx-auto mt-3 max-w-xl text-xs">{result.warnings.join(" · ")}</div> : null}
                  </TableCell></TableRow>
                ) : contacts.map((contact) => <ContactRow key={contact.id} contact={contact} />)}
              </TableBody>
            </Table>
          </div>
          {totalCount > 0 && (
            <div className="flex items-center justify-between border-t px-4 py-3 text-xs text-muted-foreground">
              <span>Showing {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, totalCount)} of {totalCount.toLocaleString()}</span>
              <div className="flex gap-1">
                <Button variant="ghost" size="sm" className="h-7 text-xs" disabled={page === 0 || loading} onClick={() => setPage((p) => Math.max(0, p - 1))}>← Prev</Button>
                <Button variant="ghost" size="sm" className="h-7 text-xs" disabled={page >= totalPages - 1 || loading} onClick={() => setPage((p) => p + 1)}>Next →</Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
