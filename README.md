# Parallax
Parallax 視差 — measures how Taiwanese outlets diverge covering the same incident: stance, coverage weight, copy propagation, and framing deltas.

## Run

CI runs Ruff lint and the full test suite (including Postgres and UI tests) on every pull request and push to `main`; a red check means lint or tests failed, or CI setup failed.

```bash
make setup && make db.up && make db.migrate   # once
make crawl && make rollup                      # tier 1 + the denominator
make enrich KEYWORD=沈伯洋 && make dedup && make framing && make stance KEYWORD=沈伯洋
make social KEYWORD=沈伯洋                     # Threads public posts (approved token required)
make report KEYWORD=沈伯洋                     # Q1–Q3 as text
make ui                                        # the same page at http://localhost:8501
```

Tier 1 must run on an always-on host (CLAUDE.md invariant 1a); the packaging
and the cutover checklist are in [ops/README.md](ops/README.md).
