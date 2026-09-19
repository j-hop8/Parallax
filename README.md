# Parallax
Parallax 視差 — measures how Taiwanese outlets diverge covering the same incident: stance, coverage weight, copy propagation, and framing deltas.

## Run

```bash
make setup && make db.up && make db.migrate   # once
make crawl && make rollup                      # tier 1 + the denominator
make enrich KEYWORD=沈伯洋 && make dedup && make framing && make stance KEYWORD=沈伯洋
make report KEYWORD=沈伯洋                     # Q1–Q3 as text
make ui                                        # the same page at http://localhost:8501
```
