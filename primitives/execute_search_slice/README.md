# execute_search_slice

Execute exactly one bounded role-search slice.

Rules:

- resolve raw company names before retrieval if needed
- do not re-decompose raw prose
- do not merge slices here
- do not run expensive scoring

This primitive can call `execute_role_search` underneath.
