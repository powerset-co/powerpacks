# Lessons 📚

> Created: 2026-06-11
>
> Change log:
> - 2026-06-11: Initial file; eval-validation lesson.

## Never grade agent/LLM output with keyword counts

**Mistake (2026-06-11):** Added `must_include` / `must_not_include` keyword
lists to skill-eval cases to validate which plan an agent produces. The operator
rejected this: keyword matching grades vocabulary, not behavior — a plan can
parrot the right tokens while doing the wrong thing, or describe the right
behavior in different words and fail.

**Rule:** When validating free-form agent or LLM output, use an LLM judge
with a plain-language `expected_behavior` rubric (or semantic similarity if
a judge is unavailable). Deterministic string assertions are fine for
structured output (JSON fields, exit codes, file paths) — never for prose.
