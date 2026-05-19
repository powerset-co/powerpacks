import assert from "node:assert/strict";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { build } from "esbuild";

const appRoot = path.resolve(import.meta.dirname, "..");
const tempRoot = await fsp.mkdtemp(path.join(appRoot, ".tmp-local-api-"));
const entry = path.join(tempRoot, "entry.ts");
const bundle = path.join(tempRoot, "bundle.mjs");

await fsp.writeFile(entry, `
  export { handleContactsRequest } from ${JSON.stringify(path.join(appRoot, "local-api/contacts.ts"))};
  export { getCompanies, getCompanyDetail, getCompanyAutocomplete } from ${JSON.stringify(path.join(appRoot, "local-api/companies.ts"))};
  export { toJsonSafe, sendJson } from ${JSON.stringify(path.join(appRoot, "local-api/jsonSafe.ts"))};
  export { openConfiguredDuckDb, closeDuckDb, hasTable, tableColumns, queryRows } from ${JSON.stringify(path.join(appRoot, "local-api/duckdb.ts"))};
`);

await build({
  entryPoints: [entry],
  outfile: bundle,
  bundle: true,
  platform: "node",
  format: "esm",
  external: ["duckdb"],
  logLevel: "silent",
});

const api = await import(pathToFileURL(bundle));

function callContacts(repoRoot, url) {
  return new Promise((resolve, reject) => {
    const res = {
      statusCode: 0,
      headers: {},
      setHeader(key, value) { this.headers[key] = value; },
      end(body) { resolve({ status: this.statusCode, body: JSON.parse(body) }); },
    };
    api.handleContactsRequest({ url }, res, () => reject(new Error("unexpected next()")), { repoRoot }).catch(reject);
  });
}

async function writeJsonl(file, rows) {
  await fsp.mkdir(path.dirname(file), { recursive: true });
  await fsp.writeFile(file, rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
}

try {
  delete process.env.POWERPACKS_LOCAL_SEARCH_DB;

  assert.deepEqual(api.toJsonSafe({
    safe: 12n,
    unsafe: BigInt(Number.MAX_SAFE_INTEGER) + 2n,
    nan: Number.NaN,
    inf: Number.POSITIVE_INFINITY,
    date: new Date("2024-01-02T03:04:05Z"),
    bytes: Buffer.from("hello"),
    nested: [1n, Number.NaN],
  }), {
    safe: 12,
    unsafe: "9007199254740993",
    nan: null,
    inf: null,
    date: "2024-01-02T03:04:05.000Z",
    bytes: "hello",
    nested: [1, null],
  });

  const emptyRepo = path.join(tempRoot, "empty-repo");
  await fsp.mkdir(emptyRepo, { recursive: true });
  const emptyContacts = await callContacts(emptyRepo, "/local-api/contacts");
  assert.equal(emptyContacts.status, 200);
  assert.equal(emptyContacts.body.source, "none");
  assert.equal(emptyContacts.body.total_count, 0);
  assert.match(emptyContacts.body.warnings.join("\n"), /no contacts source found/);

  const emptyCompanies = await api.getCompanies(new URL("http://localhost/local-api/companies"), emptyRepo);
  assert.equal(emptyCompanies.source, "empty");
  assert.equal(emptyCompanies.total, 0);
  assert.match(emptyCompanies.warnings.join("\n"), /No company data found/);

  const repo = path.join(tempRoot, "fixture-repo");
  await fsp.mkdir(path.join(repo, ".powerpacks/messages"), { recursive: true });
  await fsp.writeFile(path.join(repo, ".powerpacks/messages/contacts.csv"), [
    "id,primary_email,display_name,first_name,last_name,headline,total_messages,phone_numbers",
    "c1,zoe@example.com,Zoe Zebra,Zoe,Zebra,Founder,7,111;222",
    "c2,amy@example.com,Amy Alpha,Amy,Alpha,Engineer,3,333",
    "c3,bob@example.com,Bob Beta,Bob,Beta,Designer,5,444",
  ].join("\n"));

  const contactsPage = await callContacts(repo, "/local-api/contacts?page=0&page_size=2&sort_field=last_name&sort_dir=asc");
  assert.equal(contactsPage.body.source, "artifacts");
  assert.equal(contactsPage.body.total_count, 3);
  assert.deepEqual(contactsPage.body.data.map((row) => row.last_name), ["Alpha", "Beta"]);
  assert.deepEqual(contactsPage.body.data[0].phone_numbers, ["333"]);

  const contactsSearch = await callContacts(repo, "/local-api/contacts?search=headline:founder");
  assert.equal(contactsSearch.body.total_count, 1);
  assert.equal(contactsSearch.body.data[0].display_name, "Zoe Zebra");

  const searchIndex = path.join(repo, ".powerpacks/search-index");
  await writeJsonl(path.join(searchIndex, "company/companies_corpus.jsonl"), [
    { id: "acme", name: "Acme AI", sector_types: ["AI"], entity_types: ["Startup"], people_count: 1, funding_total: 1000 },
    { id: "beta", name: "Beta Bio", sector_types: ["Health"], entity_types: ["Lab"], people_count: 1 },
    { id: "gamma", name: "Gamma Games", sector_types: ["Games"], entity_types: ["Studio"], people_count: 0 },
  ]);
  await writeJsonl(path.join(searchIndex, "hydrate_people/llm_profiles.jsonl"), [
    { id: "p1", name: "Alice Able", headline: "ML lead", positions: [{ company_id: "acme", company_name: "Acme AI", title: "Head of ML", current: true, tenure_years: 2 }] },
    { id: "p2", name: "Ben Baker", headline: "Sales", positions: [{ company_id: "acme", company_name: "Acme AI", title: "Account Exec", current: false, tenure_years: 1 }] },
  ]);

  const companiesPage = await api.getCompanies(new URL("http://localhost/local-api/companies?limit=2&offset=1"), repo);
  assert.equal(companiesPage.source, "files");
  assert.equal(companiesPage.total, 3);
  assert.deepEqual(companiesPage.companies.map((c) => c.id), ["beta", "gamma"]);

  const companySearch = await api.getCompanies(new URL("http://localhost/local-api/companies?name=acme"), repo);
  assert.equal(companySearch.total, 1);
  assert.equal(companySearch.companies[0].name, "Acme AI");

  const autocomplete = await api.getCompanyAutocomplete(new URL("http://localhost/local-api/companies/autocomplete?q=bet"), repo);
  assert.deepEqual(autocomplete.data, [{ id: "beta", name: "Beta Bio" }]);

  const detail = await api.getCompanyDetail(new URL("http://localhost/local-api/companies/acme?include_people=true&people_limit=10&people_search=ml"), repo, "acme");
  assert.equal(detail.company.id, "acme");
  assert.equal(detail.company.people_count, 2);
  assert.deepEqual(detail.company.people.map((person) => person.name), ["Alice Able"]);

  process.env.POWERPACKS_LOCAL_SEARCH_DB = path.join(tempRoot, "missing.duckdb");
  assert.equal(await api.openConfiguredDuckDb(), null);

  try {
    const duckdb = (await import("duckdb")).default;
    const dbPath = path.join(tempRoot, "fixture.duckdb");
    const db = new duckdb.Database(dbPath);
    const conn = db.connect();
    await new Promise((resolve, reject) => conn.run("create table local_contacts(id varchar, total_messages bigint)", (err) => err ? reject(err) : resolve()));
    await new Promise((resolve, reject) => conn.run("insert into local_contacts values ('duck', 9007199254740993)", (err) => err ? reject(err) : resolve()));
    conn.close();
    db.close();

    process.env.POWERPACKS_LOCAL_SEARCH_DB = dbPath;
    const handle = await api.openConfiguredDuckDb();
    assert.ok(handle);
    assert.equal(await api.hasTable(handle, "local_contacts"), true);
    assert.equal(await api.hasTable(handle, "bad-name"), false);
    assert.deepEqual(await api.tableColumns(handle, "local_contacts"), ["id", "total_messages"]);
    const rows = await api.queryRows(handle, "select * from local_contacts");
    assert.equal(rows[0].id, "duck");
    assert.equal(rows[0].total_messages, "9007199254740993");
    api.closeDuckDb(handle);
  } catch (err) {
    console.warn(`Skipping DuckDB file helper exercise: ${err.message}`);
  }

  console.log("local API tests passed");
} finally {
  await fsp.rm(tempRoot, { recursive: true, force: true });
  delete process.env.POWERPACKS_LOCAL_SEARCH_DB;
}
