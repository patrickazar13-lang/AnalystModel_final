# Factor-Adjusted Analyst Scoring Design

**Date:** 2026-08-13
**Status:** Approved for implementation

---

## Summary

Add a Fama-French factor-alpha component to the analyst reliability scoring system as a **non-breaking augmentation**. Existing scores, CSVs, and Excel output remain unchanged. A new factor-adjusted score and separate Excel export are produced.

---

## Problem

The current analyst reliability score (`reliability_composite`) measures:
- Accuracy (mean |FE|)
- Predictability (NN R²)
- Consistency (residual std)
- Freshness (staleness days)

All are forecast-error based. The paper's industry Long-Short strategy is risk-adjusted against Fama-French factors (FF3+MOM, alpha = 0.62%, t = 2.48). There is no per-analyst factor adjustment — an analyst could appear skilled but merely be loading on market/SMB/HML/MOM risk factors.

---

## Solution: Hybrid Augmentation (Option D)

### New Component: `factor_alpha_score`

For each analyst with sufficient history:
1. Take their **forecast-error residual** = `fe - nn_predicted_fe` (the paper's "sentiment" — unpredictable component)
2. Convert to monthly frequency (forward-fill quarterly → monthly, matching paper's monthly rebalancing)
3. Regress on FF3+MOM or FF5+MOM using existing `regress_on_factors()` in `factors.py`
4. `factor_alpha_score = z(alpha_monthly_pct)` — z-scored across analysts with valid alphas

### Handling Insufficient Data (Critical)

| Scenario | Handling |
|----------|----------|
| Analyst has < `MIN_FACTOR_OBS` (default 60 monthly ≈ 5 years) | `factor_alpha_score = NaN`; excluded from z-mean for that component (same as missing `freshness_score`) |
| Single-ticker `--live` run (~12 quarters) | All analysts get `NaN`; warning printed; meaningful only on multi-ticker universe |
| Analyst has factor alpha but < `CREDIBILITY_PRIOR_OBS` (10) observations | Credibility weight `n/(n+10)` shrinks the factor-alpha component toward 0 (neutral) — same as other components |

**This is additive, not replacement.** Analysts with insufficient factor data still get their 4-component score.

### New Composite: `factor_adjusted_composite`

```
z_source_cols = [accuracy_z, predictability_z, consistency_z, freshness_z?, factor_alpha_z?]
raw_composite = mean(z_source_cols)  # skips NaN per row automatically
factor_adjusted_composite = raw_composite * credibility_weight
```

Where `credibility_weight = n_predictions / (n_predictions + CREDIBILITY_PRIOR_OBS)` — same formula, now also weights the factor-alpha component.

---

## Outputs (Non-Breaking)

| File | Change |
|------|--------|
| `outputs/live_<TICKER>_analyst_scores.csv` | **Unchanged** — still has `reliability_composite` |
| `outputs/live_<TICKER>_factor_adjusted_scores.csv` | **New** — adds: `factor_alpha`, `factor_alpha_tstat`, `factor_alpha_z`, `factor_adjusted_composite`, `loading_Mkt-RF`, `loading_SMB`, `loading_HML`, `loading_MOM` (+ RMW, CMA if FF5+MOM) |
| `export_to_excel.py` | **Unchanged** |
| `export_factor_adjusted_excel.py` | **New** — mirror of `export_to_excel.py` with: `FactorAlpha` sheet (per-analyst regressions), `Leaderboard` adds `Factor Alpha` column |

---

## Files to Create/Modify

### 1. `src/factors.py` — New Function
```python
def compute_analyst_factor_alpha(
    analyst_fe_panel: pd.DataFrame,      # columns: [analyst, firm, quarter, fe, nn_predicted_fe]
    factor_model: str = "FF3+MOM",
    min_obs: int = 60,
    use_cache: bool = True,
    download_missing: bool = False,
) -> pd.DataFrame:
    """
    Returns DataFrame indexed by analyst with columns:
    [factor_alpha, factor_alpha_tstat, factor_alpha_annualized,
     loading_Mkt-RF, loading_SMB, loading_HML, loading_MOM, (+RMW, CMA),
     n_obs, r_squared]
    Analysts with < min_obs get NaN rows (preserved in index for alignment).
    """
```

### 2. `master_pipeline.py` — New Functions + CLI Flag
```python
def compute_factor_adjusted_scores(
    predicted: pd.DataFrame,             # output of run_expanding_window()
    factor_model: str = "FF3+MOM",
    min_factor_obs: int = 60,
) -> pd.DataFrame:
    """
    Merges factor-alpha results into analyst_reliability_scores() output,
    adds factor_alpha_z, factor_adjusted_composite columns.
    Returns full scores DataFrame (backward compatible + new cols).
    """
```

CLI: `--factor-adjusted` flag triggers this computation and writes the new CSV.

### 3. `export_factor_adjusted_excel.py` — New Script
- Mirrors `export_to_excel.py` structure
- Adds `FactorAlpha` sheet with per-analyst regression table
- `Leaderboard` sheet adds `Factor Alpha` and `Factor Adj Score` columns
- All formulas reference `Raw Data` sheet (same as current)

### 4. Tests
- `factors.py` selfcheck already validates `regress_on_factors()` recovers injected alpha
- New: `test_factor_adjusted_scoring.py` with synthetic multi-analyst panel where some analysts have known factor structure → assert recovery within tolerance

---

## Data Flow

```
raw_fe (analyst, firm, quarter, fe, ...)
    → run_expanding_window()
    → predicted (adds nn_predicted_fe, linear_predicted_fe)
    → winsorize
    → consensus → industry_level() / market_level()
    → analyst_reliability_scores() → reliability_composite (UNCHANGED)
    → NEW: compute_factor_adjusted_scores()
        → compute_analyst_factor_alpha() [uses factors.py load_factors + regress_on_factors]
        → merge factor_alpha → z-score → factor_adjusted_composite
        → write factor_adjusted_scores.csv
```

---

## Backward Compatibility Guarantees

1. `analyst_reliability_scores()` signature and output unchanged
2. `simple_accuracy_leaderboard()` unchanged
3. `run_pipeline()` default behavior unchanged (no factor backtest unless `--factor-backtest` or `--factor-adjusted`)
4. `export_to_excel.py` produces identical workbooks
5. Existing `--mock` runs work identically (new CSV has all NaN factor columns with warning)
6. All existing CSV schemas preserved

---

## Configuration

| Constant | Location | Default | Rationale |
|----------|----------|---------|-----------|
| `MIN_FACTOR_OBS` | `src/config.py` | 60 | ~5 years monthly for stable 4-6 factor OLS |
| `FACTOR_MODEL` | `src/config.py` | "FF3+MOM" | Matches paper's "four-factor alpha" |
| `K_PER_LEG` | `src/config.py` | 5 | Paper's strategy |
| `STRATEGY_WEIGHTS` | `src/config.py` | "equal" | Interim default |

---

## Success Criteria

1. **Mock run** (`python master_pipeline.py --mock --factor-adjusted`):
   - Runs without error
   - Writes `outputs/mock_factor_adjusted_scores.csv`
   - Factor columns are NaN (expected: synthetic data has no real factor structure, < 60 obs)
   - Existing `mock_analyst_scores.csv` identical to before

2. **Single-ticker live run** (`python master_pipeline.py --live --ticker AAPL-US --factor-adjusted`):
   - Runs without error
   - Prints warning: "Insufficient observations for factor-alpha (need 60 monthly, have ~12)"
   - New CSV has NaN factor columns
   - Existing outputs unchanged

3. **Multi-ticker universe** (future):
   - Analysts with ≥60 monthly obs get valid factor_alpha
   - `factor_adjusted_composite` differs meaningfully from `reliability_composite`
   - Excel export includes FactorAlpha sheet with valid regressions

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Per-analyst factor regression unstable with <60 obs | Hard guard: return NaN, print warning, exclude from z-mean |
| Breaking existing scoring | No changes to `analyst_reliability_scores()`; new function wraps it |
| Excel formula breakage | New script, new workbook; old workbook untouched |
| Factor model mismatch (FF3 vs FF5) | Reuses existing `load_factors()` and `regress_on_factors()`; CLI flag `--factor-model` works for both |
| Quarterly → monthly conversion bias | Forward-fill within quarter matches paper's monthly rebalancing assumption; documented |