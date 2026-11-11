# Refine a finished search

Created: 2026-11-11

Change log:
- 2026-11-11: First version. The cloud agent's two refine tools (`query_results` SQL and
  `score_results`), ported: `rerank --sql` and `rerank --criteria`, with the cloud's routing rule,
  table schema, DuckDB notes and examples, and the enum values as the local data really spells them.

Two tools narrow or re-order the people a quick search already found. Both run through
`rerank --run-dir .powerpacks/search/<run_id>` with the standard command prefix, in the background,
then one line to the user. `<run_id>` is the id in the chat's "About the search saved in
.powerpacks/deep-search/<run_id>" line. "Undo" / "go back to …" is `rerank --run-dir … --use <step>`,
the step chosen from `steps` in `.powerpacks/deep-search/<run_id>/results.json` by matching the user's
words to `criteria`; the original search is `s1`. Never show step ids to the user. New retrieval
terms (a new place, company, "also include") need a new `prepare`.

## Which tool

Ask: can this be answered by checking a field value (a name, a number, a category)?

- **Yes → SQL**: `--criteria '<the user's words>' --sql '<DuckDB SELECT>'`. Free, exact, instant.
  "only engineers", "remove founders", "Stanford grads", "under 35", "VP and above", "series B or
  later", "in San Francisco", "10+ years experience", "sort by age / headcount / funding / tenure".
- **No → the judge**: `--criteria '<the user's words>'` alone. It reads every profile with OpenAI and
  costs money, so never use it for a criterion SQL can answer. Ability, potential, fit, quality,
  relevance: "strongest AI background", "best fit for a VP role", "most entrepreneurial", "who has
  X experience" (semantic; title ILIKE catches false positives from shared keywords).

`inferred_age` **is the person's age** (94% of rows have it). "Under 35" is
`SELECT person_id FROM candidates WHERE inferred_age < 35`. Never demand explicit age evidence.

## The tables

The query runs over the rows showing. It must return `person_id`; the rows it returns stay, in its
order (rows dropped → the step is a filter; none dropped → a sort). A `reasoning` column, when
returned, shows beside each person's name, so return one: a short label with units
(`'age 28'`, `'3.5 years exp'`, `'Stanford — BS'`, `'$1.2B raised'`).

```sql
candidates(person_id, name, headline, summary, location, all_current_titles,
           linkedin_followers, linkedin_connections, x_twitter_followers,
           total_interactions, inferred_age, inferred_birth_year, total_years_experience,
           tech_skills VARCHAR[], base_score)
-- all_current_titles: 'Founder at X | Engineer at Y' (every current position); use it for
-- exclusions: WHERE all_current_titles NOT ILIKE '%founder%' AND all_current_titles NOT ILIKE '%ceo%'
-- follower counts are null for most rows of the local data; sort with NULLS LAST.

positions(person_id, position_index, is_matched, is_current,
          position_title, company_name, company_domain, seniority_band, role_track,
          company_headcount, company_funding_total, company_stage,
          company_sector_types VARCHAR[], company_entity_types VARCHAR[], investor_names VARCHAR[],
          location, start_date, end_date, tenure_years)
-- is_matched: the position the original search matched.

education(person_id, school_name, degree, field_of_study, start_year, end_year, graduation_year)

trait_scores(person_id, trait_name, score)
```

Values, as the data spells them (already lower-cased):
- `seniority_band`: trainee, junior, entry, mid, ic, senior, senior_ic, senior_individual_contributor,
  staff, principal, manager, director, c-suite, owner. "VP and above" ≈ `('director','c-suite','owner')`.
- `company_stage`: pre_seed, seed, series_a, series_b, series_c, series_d, early, growth, mature,
  exited, venture_unknown, other, or empty.
- `company_entity_types`: venture_backed_startup, nonprofit, government_public_sector, bank, vc_firm,
  club_association, insurance_carrier, pe_firm, foundation_endowment, family_office.
- `company_sector_types`: saas, hardware, marketplaces, ai_ml, social_networking, creator_tools,
  infra_devtools, data, fintech, deep_tech, commerce_tech, devops, cybersecurity, marketing_tech,
  health_tech, and more; use `list_contains`.

## DuckDB notes

- `ILIKE` with `%` between every word: `'%software%engineer%'`, `'%civil%engineering%'`. Titles,
  companies and schools vary; exact phrases miss.
- Lists: `list_contains(arr, 'val')`, `list_has_any(arr, ['a','b'])`,
  `len(list_filter(arr, x -> x ILIKE '%pat%')) > 0`.
- Dates are `'YYYY-MM'`: `DATE_DIFF('year', (start_date || '-01')::DATE, CURRENT_DATE)`.
- Sorting: `NULLS LAST`, never `COALESCE(x, 0)` (that invents zeros). Reasoning for a null:
  `CASE WHEN x IS NULL THEN 'age unknown' ELSE 'age ' || x END`.
- Tenure at a company is the SUM over that company's positions (promotions split it).
- Sorting by a positions column: aggregate per person (MAX/SUM with GROUP BY) and order by that;
  `DISTINCT` + `ORDER BY` on a joined positions table picks an arbitrary row.
- A SQL error comes back as the failed command's error text; fix the query and run again.

## Examples

```sql
-- under 35
SELECT person_id, 'age ' || inferred_age AS reasoning FROM candidates WHERE inferred_age < 35

-- youngest first
SELECT person_id, CASE WHEN inferred_age IS NULL THEN 'age unknown' ELSE 'age ' || inferred_age END AS reasoning
FROM candidates ORDER BY inferred_age ASC NULLS LAST

-- Stanford
SELECT DISTINCT c.person_id, e.school_name || COALESCE(' — ' || e.degree, '') AS reasoning
FROM candidates c JOIN education e USING (person_id) WHERE e.school_name ILIKE '%stanford%'

-- current title and place
SELECT DISTINCT c.person_id, p.position_title || ' at ' || p.company_name || ' · ' || c.location AS reasoning
FROM candidates c JOIN positions p USING (person_id)
WHERE p.is_current AND p.position_title ILIKE '%engineer%' AND c.location ILIKE '%san%francisco%'

-- remove founders and CEOs
SELECT person_id, all_current_titles AS reasoning FROM candidates
WHERE all_current_titles NOT ILIKE '%founder%' AND all_current_titles NOT ILIKE '%ceo%'

-- series B or later, now
SELECT DISTINCT c.person_id, p.company_name || ' (' || p.company_stage || ')' AS reasoning
FROM candidates c JOIN positions p USING (person_id)
WHERE p.is_current AND p.company_stage IN ('series_b','series_c','series_d','growth','mature','exited')

-- 3+ years at the matched company (sum over promotions)
SELECT c.person_id, t.company_name || ' — ' || ROUND(t.total, 1) || ' years' AS reasoning
FROM candidates c JOIN (
  SELECT p.person_id, p.company_name, SUM(p.tenure_years) AS total FROM positions p
  WHERE p.company_name IN (SELECT company_name FROM positions WHERE is_matched AND person_id = p.person_id)
  GROUP BY 1, 2) t USING (person_id)
WHERE t.total >= 3

-- biggest current company first
SELECT c.person_id, printf('%,d', MAX(p.company_headcount)) || ' employees' AS reasoning
FROM candidates c JOIN positions p USING (person_id) WHERE p.is_current
GROUP BY c.person_id ORDER BY MAX(p.company_headcount) DESC NULLS LAST

-- 10+ years of experience
SELECT person_id, ROUND(total_years_experience, 1) || ' years exp' AS reasoning
FROM candidates WHERE total_years_experience >= 10
```
