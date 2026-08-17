# SDD ledger — plan: docs/superpowers/plans/2026-08-13-factor-adjusted-analyst-scoring.md

## Rulings

- **Ruling 1 — No git repo:** The project has no `.git` anywhere (checked `analyst-model` and parent). Plan steps that say `git commit` are skipped. Progress is tracked here instead; the per-task test/verification gates from the plan are preserved and run inline.

## Pre-flight conflict scan

| Task pair | Shared interface | Finding |
|-----------|------------------|---------|
| T1→T2 | `MIN_FACTOR_OBS` constant | Clean — T2 uses it as default `min_obs` |
| T2→T3 | `compute_analyst_factor_alpha()` signature & output columns | Clean — T3 consumes indexed-by-analyst DataFrame |
| T3→T4 | `factor_adjusted_scores.csv` schema | Clean — T4 reads the exact columns T3 writes |
| T4→T5 | exporter + mock CSVs | **Note:** mock run writes `mock_*` files, but exporter hardcodes `live_<TICKER>_*`. T5 must handle this mismatch (plan flags it). |
| T2 tests | `load_factors("FF3+MOM")` may need real factor data | Clean — test has a synthetic fallback path |

## Progress

- [x] Task 1: Add MIN_FACTOR_OBS to config.py
- [x] Task 2: Add compute_analyst_factor_alpha() to factors.py + tests
- [ ] Task 3: Add compute_factor_adjusted_scores() + CLI flag to master_pipeline.py
- [ ] Task 4: Create export_factor_adjusted_excel.py
- [ ] Task 5: Integration test & README update
- [ ] Task 6: Regression verification
