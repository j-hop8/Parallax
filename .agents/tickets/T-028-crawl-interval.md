# T-028 — Crawl every 10 minutes: udn outruns a 20-minute poll

**Owner:** claude — it trades fetch tolerance for coverage, and the numbers
behind that trade had to be measured rather than guessed. **Blocked by:** nothing.

## Why

T-022's saturation detector, on its first day of real data, found the crawl
losing articles on ordinary days:

```
udn: at_risk=1  median=25.9%  worst=80.8%  fastest_turnover=13.6 min
ftv: median=20.6%  worst=64.7%  fastest_turnover=14.3 min
```

udn replaced **80.8% of its feed window inside a single 20-minute cycle**, and
its fastest observed full turnover is **13.6 minutes** against a 20-minute
poll. Articles published in that window scroll out before the next crawl sees
them, which is permanent (invariant 1) and lands in the **Q2 denominator** --
so udn's coverage weight is currently inflated by its own undercount.

## Design — decided

**Interval 1200 -> 600 seconds**, in both `ops/com.parallax.crawl.plist.template`
(launchd) and `ops/systemd/parallax-crawl.timer` (the Linux host), which must
not drift apart.

`tests/test_crawl_health.py::test_worst_case_crawl_cycle_fits_the_launchd_interval`
refused the change, correctly: a fully hung cycle took **924s against a 600s
interval**. A cycle that outlives its own slot makes launchd defer the next one
and the schedule silently drifts. So the worst case had to come down with it.

Measured against the shipped config, only one knob could close that gap:

| timeout | budget | netwait | worst cycle | fits 600s |
|---|---|---|---|---|
| 20 | 180 | 120 | 924s | no (current) |
| 20 | 90 | 120 | 834s | no |
| 15 | 90 | 60 | 654s | no |
| **10** | **90** | **60** | **534s** | **yes** |

`timeout_seconds` **20 -> 10**, `budget_seconds` **180 -> 90**,
`wait_for_network` default **120 -> 60**. Budget and network-wait alone cannot
do it at any setting; the timeout has to move.

**Why 10s is safe here, from observed runs rather than intuition:**

| outlet | feeds | avg | max |
|---|---|---|---|
| cna | 11 | 21.3s | 22.1s |
| ettoday | 1 | 1.7s | 1.9s |
| everything else | 0-1 | ≤0.4s | ≤1.3s |

cna's 21s is **11 requests at a 2s rate limit** -- deliberate politeness, not
network time. No single request comes close to 10s; a real cycle is ~24s, i.e.
4% of the interval. The 924s figure is the pathological case where every host
accepts a connection and then hangs.

## Not changed, deliberately

`rollup_daily`'s `max_gap_minutes` (90) still defines a "complete" day. It is a
statement about wall-clock coverage, not about cycle count, and a 90-minute
hole loses the same articles at either interval. Tightening it would change
which days qualify as Q2 denominators -- a separate decision, not a side effect
of this one.

## Files in scope

`ops/com.parallax.crawl.plist.template`, `ops/systemd/parallax-crawl.timer`,
`config/outlets.yaml`, `src/parallax/crawl/http.py`,
`src/parallax/jobs/crawl_listing.py`, and the comments in `Makefile`,
`ops/README.md`, `src/parallax/crawl/listing.py`,
`src/parallax/jobs/rollup_daily.py` that stated "every 20 minutes".

## Acceptance criteria

- launchd and systemd both say 10 minutes; neither is left behind.
- The worst-case cycle test passes against the 600s interval.
- `plutil -lint` accepts the substituted plist and `make ops.check` still
  verifies the systemd units.
- A live crawl takes all 8 outlets under the tighter budget.
- `launchctl print` reports `run interval = 600 seconds` after `make sched.install`.

## Verify

```bash
uv run pytest -q tests/test_crawl_health.py && make ops.check && uv run pytest -q
```

## Shipped

408 tests pass, ruff clean, `make ops.check` OK, live crawl took all 8 outlets,
and `launchctl print gui/$UID/com.parallax.crawl` reports
`run interval = 600 seconds`.
