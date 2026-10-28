// The connections count is read by shape, whatever language LinkedIn shows the page in.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { shownCount } = require("../packs/ingestion/primitives/discover/linkedin/count.js");

const CHROME = ["Manage my network", "Sort by:", "Recently added", "Search by name", "Show results"];

// The live connections page, read 2026-10-28 with the account on English and on Spanish:
// the count is the first text in <main>, then the sort controls, then the cards.
const ENGLISH = ["298 connections", "Sort by:", "Sort by: Recently added", "Recently added", "Search with filters",
  "Bruce Wang", "The non-break career break", "Connected on September 22, 2026", "Message"];
const SPANISH = ["298 contactos", "Ordenar por:", "Ordenar por: Añadidos recientemente", "Añadidos recientemente",
  "Buscar con filtros", "Bruce Wang", "The non-break career break", "Contacto desde 22 de septiembre de 2026", "Enviar mensaje"];

test("the live page's lines give the count in English and in Spanish", () => {
  assert.equal(shownCount(ENGLISH), 298);
  assert.equal(shownCount(SPANISH), 298);
  // The rule this replaces: the English word, which the Spanish page never shows.
  assert.equal(SPANISH.find((text) => /^[\d,.]+ connections?$/i.test(text)), undefined);
});

test("the count line is found in any language", () => {
  for (const [line, total] of [
    ["1,234 connections", 1234],
    ["1 connection", 1],
    ["1.234 contactos", 1234],
    ["1.234 Kontakte", 1234],
    ["1 234 relations", 1234],
    ["つながり1,234人", 1234],
    ["Connexions (1 234)", 1234],
  ]) {
    assert.equal(shownCount([...CHROME, line, "Ana García", "Connected on 12 Oct 2025"]), total, line);
  }
});

test("names, dates, and long text are not the count", () => {
  assert.equal(shownCount([...CHROME, "Ana García", "Connected on 12 October 2025", "Conectado el 12 de octubre de 2025"]), null);
  assert.equal(shownCount(["3", "Engineer at 3M since 2020, building 10x teams for 1,000 customers"]), null);
  assert.equal(shownCount(["١٢٣ connections"]), null);
  assert.equal(shownCount([]), null);
});
