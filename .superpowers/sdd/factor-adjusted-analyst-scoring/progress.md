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

- [x] Task 1: Add MIN_FACTOR_OBS to config.py -- **revised**: was 60 (monthly),
      now 10 (quarterly) -- see below.
- [x] Task 2: Add compute_analyst_factor_alpha() to factors.py + tests
- [x] Task 3: Add compute_factor_adjusted_scores() + CLI flag to master_pipeline.py
      -- was actually already written but only wired into `--mock`/`--live`, not
      `--from-outputs` (the mode that pools enough analyst history to matter);
      fixed. `--min-factor-obs` CLI override added.
- [ ] Task 4: Create export_factor_adjusted_excel.py -- still not started.
- [ ] Task 5: Integration test & README update -- tests updated (see below), README not yet.
- [ ] Task 6: Regression verification

## Post-hoc fixes (found while actually running this against real multi-ticker data)

1. **Methodological bug in compute_analyst_factor_alpha()**: forward-filled each
   quarterly residual into 3 identical monthly copies to reach a 60-observation
   (5-year) bar. This manufactures 3 fully-correlated, non-independent rows out
   of 1 real observation -- deflates the regression's true standard errors, and
   no analyst in this project's actual 2-3 year pulls could ever clear 60
   monthly obs anyway. Fixed: regresses directly at the natural quarterly
   frequency against quarter-compounded factor returns. `MIN_FACTOR_OBS`
   dropped from 60 (monthly) to 10 (quarterly) to match -- same "trust nobody
   until ~10 quarters" threshold this project already uses elsewhere
   (CREDIBILITY_PRIOR_OBS, MIN_TRAINING_OBS). `factor_alpha_annualized` now
   multiplies by 4 (quarterly -> annual), not 12.
2. **NaN-propagation bug**: `residual = fe - nn_predicted_fe` produces NaN for
   any analyst without a trained NN prediction (fe - NaN = NaN), which silently
   dropped ~99% of analysts before they ever reached the quarterly aggregation
   step -- independent of the 60-vs-10 threshold. Fixed: `nn_predicted_fe`
   defaults to 0 when missing, so an un-modeled analyst's raw forecast error
   becomes her residual (nothing systematic has been netted out yet, which is
   the honest state of affairs).
3. **compute_factor_adjusted_scores()'s base (analyst_reliability_scores())
   requires the paper's NN-training bar (>=10 LAGGED observations) -- much
   stricter than factor-alpha's own bar, and on real data this leaves ~1
   analyst with a score even when 6+ have enough history for a real
   factor_alpha.** Added `factor_adjusted_partial_leaderboard()`, built on the
   NN-independent `simple_accuracy_leaderboard()` instead (same "full vs
   partial" duality this project already uses for the non-factor score). Both
   versions now run and write on every `--factor-adjusted` call, via a new
   shared `_run_and_write_factor_adjusted()` helper (avoids the 3x code-drift
   risk from `--mock`/`--live`/`--from-outputs` each hand-rolling the same logic).

Verified: `tests/test_factor_adjusted_scoring.py` rewritten for the quarterly
model and passing; a fresh synthetic-alpha-recovery script (not committed,
ad hoc) confirmed the OLS recovers known injected alpha/loadings against real
cached quarterly factor data; ran against the real 12-ticker `--from-outputs`
panel -- partial leaderboard surfaces 6 analysts with a real factor_alpha out
of 191 total (full NN-gated version still only 1, expected given data depth).
