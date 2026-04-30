# Semantic Query Examples

Use this as calibration when generating `role_search_filters.semantic_query`.
These examples are adapted from Aleph's role extraction prompts and role-hint
files. They are references, not templates to copy blindly.

## Rule

`semantic_query` describes what the target person does and what profile evidence
would make them relevant for semantic retrieval. It is not a title list, entity
list, location filter, or Boolean constraint.

Put title aliases, acronyms, and short lexical variants in `bm25_queries`.

## Examples

Software engineering:

```json
{
  "semantic_query": "Builds and maintains software products or systems, implements features, debugs issues, reviews code, and contributes to technical design. Works hands-on with application, backend, frontend, mobile, platform, infrastructure, or systems code in production environments.",
  "bm25_queries": ["software engineer", "software developer", "backend engineer", "frontend engineer", "full stack engineer", "SWE", "SDE"]
}
```

Machine learning engineering:

```json
{
  "semantic_query": "Builds, deploys, and improves machine learning systems in production. Designs model pipelines, ML infrastructure, evaluation workflows, and MLOps practices, translating research or data science work into reliable products and services.",
  "bm25_queries": ["machine learning engineer", "ML engineer", "MLOps engineer", "ML platform engineer", "applied scientist"]
}
```

Data science:

```json
{
  "semantic_query": "Analyzes data to extract insights, builds predictive or statistical models, applies machine learning or experimentation methods, and communicates findings to stakeholders. Works with large datasets to inform product, business, risk, growth, or operational decisions.",
  "bm25_queries": ["data scientist", "applied scientist", "research scientist", "statistician", "quantitative researcher"]
}
```

Product management:

```json
{
  "semantic_query": "Defines product strategy, prioritizes roadmaps, works with engineering and design, analyzes customer needs, and drives product decisions from discovery through launch. Balances user problems, business goals, technical tradeoffs, and execution to ship products.",
  "bm25_queries": ["product manager", "PM", "technical product manager", "product owner", "head of product", "CPO"]
}
```

Founder:

```json
{
  "semantic_query": "Started, founded, or built a company from scratch, took entrepreneurial risk, made early strategic decisions, hired initial teams, raised funding or bootstrapped, and owned company-building outcomes. Profile evidence may include founder, co-founder, founding executive, or founding team experience.",
  "bm25_queries": ["founder", "co-founder", "cofounder", "founding", "CEO", "chief executive officer"]
}
```

Sales and revenue:

```json
{
  "semantic_query": "Sells products or services, manages pipeline, builds customer relationships, negotiates contracts, closes deals, and is accountable for revenue outcomes. May work across account executive, sales development, enterprise sales, sales leadership, revenue operations, or business development motions.",
  "bm25_queries": ["account executive", "sales development representative", "BDR", "SDR", "head of sales", "VP sales", "CRO"]
}
```

Go-to-market leadership:

```json
{
  "semantic_query": "Drives go-to-market strategy and commercial execution across sales, marketing, growth, revenue, partnerships, or business development. Owns pipeline, positioning, product launches, pricing, customer acquisition, and revenue strategy across teams.",
  "bm25_queries": ["GTM leader", "go-to-market", "chief revenue officer", "VP sales", "head of marketing", "head of growth"]
}
```

Operations:

```json
{
  "semantic_query": "Runs day-to-day business operations, improves processes and workflows, coordinates cross-functional execution, and scales operational systems. Focuses on operating cadence, execution quality, business operations, strategy operations, program management, or company infrastructure.",
  "bm25_queries": ["operations manager", "business operations", "biz ops", "COO", "chief of staff", "program manager", "strategy and operations"]
}
```

Engineering leadership:

```json
{
  "semantic_query": "Leads engineering teams or technical organizations, makes technical and organizational decisions, mentors engineers, defines technical strategy, manages roadmaps, and helps hire and grow engineering talent. Profile evidence may include engineering management, technical leadership, architecture ownership, or executive engineering responsibility.",
  "bm25_queries": ["engineering manager", "tech lead", "director of engineering", "VP engineering", "head of engineering", "CTO"]
}
```

Domain-only query:

```json
{
  "semantic_query": "Has direct professional experience in the target domain, with responsibilities, products, customers, tools, or operating context specific to that domain. The profile should contain evidence of domain-relevant work, not just employment at an unrelated company.",
  "bm25_queries": ["domain keywords", "common role titles in the domain", "specialized tools or techniques"]
}
```

## Adaptation Notes

- Add seniority scope only when the user asked for it.
- Add domain responsibilities when the role is domain-specific.
- Do not concatenate sector and title into fake titles.
- Do not put company names, school names, or location names into
  `semantic_query` unless they change the meaning of the work itself.
- Company, education, location, tenure, age, and YOE should normally be
  expressed as structured filters or prefilters.
