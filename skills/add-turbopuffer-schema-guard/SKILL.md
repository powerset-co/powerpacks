# Add TurboPuffer Schema Guard

Install and maintain the schema guardrails for TurboPuffer usage.

## Intent

Prevent the agent from making common mistakes with:

- wrong attribute names
- wrong operator names
- wrong scalar types
- mixing raw company names with execute-time company IDs
- dropping recall-style constraints during execute

## Core Rule

When querying TurboPuffer, the agent must treat the Powerpacks schema docs as a
hard contract, not as suggestions.

## Querying Rules

- use only documented fields
- use only documented operators
- keep location filters on person records separate from company-name resolution
- use `ContainsAny` only for array-backed fields
- use `In` for scalar categorical fields
- use `Gte` and `Lte` for numeric bounds
- if uncertain about a field, stop and inspect the schema doc rather than guess

## Source Of Truth

- `powerpacks/docs/turbopuffer-contract.md`
- `powerpacks/schemas/role-search-filters.schema.json`
