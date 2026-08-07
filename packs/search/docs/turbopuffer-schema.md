# TurboPuffer physical schema

Powerpacks uses fixed TurboPuffer namespace contracts. The `attributes` and
`filters` sections of the checked-in JSON under
[`contracts/turbopuffer/`](../contracts/turbopuffer/) are the source of truth
for physical names, document grain, operators, and value types. Current
orchestration comes from the `$search` skill and CLI, not free-text workflow
rules retained in older contract versions. Normal search runs do not query live
namespace schemas or maintain a second handwritten attribute inventory here.
Corpus snapshots are the exception: they enumerate live schemas so
their identity covers every actual stored attribute while validating the
contract fields required for filtering, text search, and vectors. That part is
cheap and happens in both snapshot modes; only the row-level enumeration behind
`verified_comparable` is skipped when the caller supplies a corpus tag.

## Namespaces

| Contract | Default namespace | Grain |
| --- | --- | --- |
| [`people.namespace.json`](../contracts/turbopuffer/people.namespace.json) | `aleph_people_v1` | One person-position document; use `base_id` for person hydration. |
| [`education.namespace.json`](../contracts/turbopuffer/education.namespace.json) | `aleph_people_education_v1` | One person-education record used for base-ID prefiltering. |
| [`summaries.namespace.json`](../contracts/turbopuffer/summaries.namespace.json) | `aleph_summaries_v1` | Search summary documents. |
| [`companies.namespace.json`](../contracts/turbopuffer/companies.namespace.json) | `aleph_companies_v1` | One company document. |
| [`schools.namespace.json`](../contracts/turbopuffer/schools.namespace.json) | Contract-defined | Canonical school resolution. |
| [`investors.namespace.json`](../contracts/turbopuffer/investors.namespace.json) | `aleph_investors_v1` | Operator-scoped investor identities and aliases. |

The TurboPuffer backend owns investor resolution in
[`resolution.py`](../backends/turbopuffer/resolution.py). The operator-scoped
namespace rebuild contract is documented with
[`build_investor_index`](../../indexing/primitives/build_investor_index/README.md).

`TURBOPUFFER_REGION` defaults to `gcp-us-central1`. `ALEPH_ENV` may add a
non-production namespace suffix. See the
[TurboPuffer query contract](turbopuffer-contract.md) for the supported
role-search payload and operator rules; a stored attribute is not automatically
a supported user-facing filter.
