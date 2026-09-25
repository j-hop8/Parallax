# T-024 — Declare `.PHONY` per rule so parallel tickets stop colliding

**Owner:** claude (done in the same branch as this ticket — the change is one
mechanical edit and its whole value is in the verification).
**Blocked by:** nothing.

## Why

The Makefile declared all phony targets on a **single line**. Every ticket that
adds a `make` target edits that one line, so any two tickets developed in
parallel conflict there — always, and always trivially.

Measured on 2026-09-23/25, shipping T-020, T-021 and T-022 concurrently:

| conflict | resolved by |
|---|---|
| T-022 `saturation` vs T-021 `posts.eval` | merge on #25 |
| main vs T-020 `label.validate` + `stance.agreement` | merge on #26 |
| main (all three) vs T-021 again | second merge on #25 |

Three resolutions of the same line, for a file where the two sides never
actually disagreed — each side simply added a name. The third one cost a round
trip through Codex, because #25 was a `codex/*` branch where the guard hook
(correctly) refuses to let Claude commit.

GNU make accumulates repeated `.PHONY:` declarations, so a target can declare
itself next to its own rule. Then adding a target touches only the two lines it
introduces, and two branches adding different targets edit different regions of
the file. The conflict class disappears rather than getting easier to resolve.

## What changed

- The monolithic `.PHONY:` line is gone. Each phony rule is preceded by its own
  `.PHONY: <name>`.
- **`dict` is deliberately not phony.** It builds `config/dict.txt.big` and is
  guarded by `test -f`; making it phony would re-download the dictionary on
  every `make setup`.
- **`label.posts` and `resegment` are now declared**, which they were not
  before. Genuine omissions, not scope creep: a stray file of either name in
  the repo root would have silently made those targets no-ops, and the whole
  point of declaring next to the rule is that a rule cannot be forgotten.

## Verified

`make` parses the result identically, checked against the real database rather
than by reading:

```
make -pn | awk '/^\.PHONY:/{print; exit}'    # before: 41 targets
                                             # after:  43 targets
diff  ->  only additions: label.posts, resegment
dict  ->  absent from .PHONY, still a file target
.DEFAULT_GOAL := help   (unchanged -- `.PHONY: help` above `help:` does not
                         become the default goal; targets beginning with `.`
                         are not eligible)
```

## Files in scope

`Makefile`, this ticket.

## Do not touch

Anything else. No recipe body, prerequisite or target name changes — the diff
adds `.PHONY:` lines and deletes one.

## Acceptance criteria

- `make -pn` reports the same phony set as before plus exactly `label.posts`
  and `resegment`.
- `dict` is not phony.
- `.DEFAULT_GOAL` is still `help`.
- No recipe line is modified.

## Verify

```bash
make -pn >/dev/null && make -pn | awk '/^\.PHONY:/{print; exit}' | tr ' ' '\n' | grep -c .
```
