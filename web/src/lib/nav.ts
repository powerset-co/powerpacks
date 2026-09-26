// The local UI's pages, in top-bar order. The review server serves each at its href.

export type PageKey = "searches" | "people";

export interface Page {
  label: string;
  href: string;
}

export const PAGES: Readonly<Record<PageKey, Page>> = {
  searches: { label: "Searches", href: "/searches" },
  people: { label: "People", href: "/people" },
};
