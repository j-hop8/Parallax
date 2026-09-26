# T-031 — Record the scope decision: social media (Q4) is version 2.0.0

**Owner:** claude (a product decision, docs only). **Blocked by:** #34
(T-030) -- merge after it. These docs describe the `PARALLAX_SOCIAL` gate
T-030 adds; landing first would document behaviour `main` does not have.

## Why

Decision 2026-09-25: Phases 2 and 3 are news only. Social media (Threads, Q4)
becomes its own release, version 2.0.0, because the Threads API read path is
currently broken and the public demo (T-029/T-030) should not show an empty
panel. The docs still describe Q4 as part of Phase 2 and milestone week 6.

## Design — decided

- Move, don't delete: the code, `social_*` tables and tests stay. T-030 hides
  Q4 behind `PARALLAX_SOCIAL` (default off) at the presentation layer.
- Social tickets form their own group: `.agents/tickets/v2.0.0/`. T-023 moves
  there with a parked note and is not delegated now.
- At go-live of the public demo, tag `v1.0.0` (the news-only line); social
  returns as `v2.0.0`.

## Files in scope

`parallax-proposal.md` (§1, §3, §5, §11), `CLAUDE.md` (intro only),
`README.md` (the `make social` line), `.agents/tickets/T-023-post-validation.md`
→ `.agents/tickets/v2.0.0/`, this ticket.

## Do not touch

Any code, tests, `db/`, `ops/`, `Makefile`.

## Acceptance criteria

- The proposal lists Q4 as v2.0.0 in §1 and §3, Phase 2 no longer carries the
  Threads bullet, a "Version 2.0.0" section holds the platform decisions
  (PTT/Dcard dropped, Facebook parked) and the open work, and milestone week 6
  no longer promises Q4.
- CLAUDE.md and README say Q4 is v2.0.0 and how it is hidden.
- T-023 lives in `.agents/tickets/v2.0.0/` with a parked note.

## Verify

```bash
git diff --stat origin/main...HEAD -- ':!*.md' | tail -1   # expect no non-markdown changes
```
