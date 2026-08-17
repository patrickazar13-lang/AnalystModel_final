# Factor-Adjusted Analyst Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Fama-French factor-alpha component to analyst reliability scoring as a non-breaking augmentation, with new CSV output and separate Excel export script.

**Architecture:** Hybrid augmentation — keep existing `reliability_composite` unchanged; add `factor_alpha_score` (z-scored factor-model alpha of forecast-error residuals) as a 5th component folded into `factor_adjusted_composite` with same credibility weighting. New function `compute_analyst_factor_alpha()` in `factors.py` uses existing `load_factors()` + `regress_on_factors()`. New wrapper `compute_factor_adjusted_scores()` in `master_pipeline.py` merges results. New CLI flag `--factor-adjusted` triggers computation. New `export_factor_adjusted_excel.py` mirrors existing exporter.

**Tech Stack:** Python 3.10+, pandas, numpy, scikit-learn (existing), openpyxl (for Excel export)

**Spec:** `docs/superpowers/specs/2026-08-13-factor-adjusted-analyst-scoring-design.md`

---

## Global Constraints

- **No breaking changes:** `analyst_reliability_scores()`, `simple_accuracy_leaderboard()`, `run_pipeline()` default behavior, `export_to_excel.py` output — all unchanged
- **MIN_FACTOR_OBS = 60** (monthly) hard guard; analysts below threshold get NaN factor columns, excluded from z-mean
- **Credibility weighting** formula unchanged: `n_predictions / (n_predictions + CREDIBILITY_PRIOR_OBS)` where `CREDIBILITY_PRIOR_OBS = 10`
- **Factor model** defaults to `FACTOR_MODEL = "FF3+MOM"` from config; `--factor-model` CLI flag works for both scoring and backtest
- **Quarterly → monthly:** Forward-fill within quarter (paper's monthly rebalancing assumption)
- **Existing selfcheck** in `factors.py` validates `regress_on_factors()` recovers injected alpha — must continue passing

---

## File Map

| File | Role | Change |
|------|------|--------|
| `src/config.py` | Add `MIN_FACTOR_OBS = 60` | Modify |
| `src/factors.py` | Add `compute_analyst_factor_alpha()` | Modify |
| `master_pipeline.py` | Add `compute_factor_adjusted_scores()`, `--factor-adjusted` CLI flag | Modify |
| `export_factor_adjusted_excel.py` | New Excel exporter (mirrors `export_to_excel.py`) | Create |
| `tests/test_factor_adjusted_scoring.py` | Unit tests for new functions | Create |

---

## Task Breakdown

### Task 1: Add MIN_FACTOR_OBS to config.py

**Files:**
- Modify: `src/config.py:98` (after `STRATEGY_WEIGHTS`)

**Interfaces:**
- Produces: `MIN_FACTOR_OBS` constant used by `factors.py` and `master_pipeline.py`

- [ ] **Step 1: Add constant to config.py**

```python
# src/config.py - add after line 107 (STRATEGY_WEIGHTS)
# --- Factor-adjusted analyst scoring ---
# Minimum monthly observations for stable per-analyst factor regression.
# 60 months (~5 years) is standard for 4-6 factor OLS; analysts with fewer
# get NaN factor_alpha and are excluded from that component's z-mean.
MIN_FACTOR_OBS = 60
```

- [ ] **Step 2: Verify import works**

```bash
cd analyst-model
python -c "from src.config import MIN_FACTOR_OBS; print(MIN_FACTOR_OBS)"
# Expected: 60
```

- [ ] **Step 3: Commit**

```bash
git add src/config.py
git commit -m "config: add MIN_FACTOR_OBS=60 for factor-adjusted scoring"
```

---

### Task 2: Add compute_analyst_factor_alpha() to factors.py

**Files:**
- Modify: `src/factors.py` (add new function after `selfcheck()`, before `if __name__ == "__main__":`)
- Test: `tests/test_factor_adjusted_scoring.py` (create new file)

**Interfaces:**
- Consumes: `analyst_fe_panel` DataFrame with columns `['analyst', 'firm', 'quarter', 'fe', 'nn_predicted_fe']`
- Produces: DataFrame indexed by analyst with columns:
  `['factor_alpha', 'factor_alpha_tstat', 'factor_alpha_annualized',
   'loading_Mkt-RF', 'loading_SMB', 'loading_HML', 'loading_MOM',
   'loading_RMW'?, 'loading_CMA'?, 'n_obs', 'r_squared']`
- Analysts with `< MIN_FACTOR_OBS` get all-NaN rows (index preserved for alignment)

- [ ] **Step 1: Write failing test**

```python
# tests/test_factor_adjusted_scoring.py
import numpy as np
import pandas as pd
import sys
sys.path.insert(0, "analyst-model")
from src.factors import compute_analyst_factor_alpha, load_factors

def test_compute_analyst_factor_alpha_insufficient_obs():
    """Analysts with < MIN_FACTOR_OBS get NaN rows, index preserved."""
    # Build panel with 2 analysts, only 10 monthly obs each (quarterly forward-filled = 10)
    rng = np.random.default_rng(42)
    quarters = [f"2020Q{q}" for q in range(1, 5)] + [f"2021Q{q}" for q in range(1, 5)]
    rows = []
    for analyst in ["ANALYST_A", "ANALYST_B"]:
        for firm in ["FIRM1"]:
            for q in quarters:
                rows.append({
                    "analyst": analyst,
                    "firm": firm,
                    "quarter": q,
                    "fe": rng.normal(0, 0.01),
                    "nn_predicted_fe": rng.normal(0, 0.005),
                })
    panel = pd.DataFrame(rows)
    
    result = compute_analyst_factor_alpha(panel, factor_model="FF3+MOM", min_obs=60)
    
    # Both analysts should be in index
    assert list(result.index) == ["ANALYST_A", "ANALYST_B"]
    # All factor columns should be NaN
    assert result["factor_alpha"].isna().all()
    assert result["n_obs"].eq(0).all()  # or whatever indicates insufficient

def test_compute_analyst_factor_alpha_recovers_injected_alpha():
    """Synthetic panel with known factor structure recovers injected alpha."""
    # Use real factor panel from load_factors (or synthetic if unavailable)
    try:
        factor_panel = load_factors("FF3+MOM").factors
    except FileNotFoundError:
        # Build synthetic factor panel matching _synthetic_factor_panel in factors.py
        rng = np.random.default_rng(0)
        idx = pd.date_range("2015-01-31", periods=120, freq="ME")
        factor_panel = pd.DataFrame({
            "Mkt-RF": rng.normal(0.5, 4.0, 120),
            "SMB": rng.normal(0.2, 2.5, 120),
            "HML": rng.normal(0.2, 3.0, 120),
            "MOM": rng.normal(0.3, 4.0, 120),
            "RF": np.full(120, 0.15),
        }, index=idx)
    
    # Build analyst residuals = alpha + X @ beta + noise
    # We'll inject alpha=0.62 for ANALYST_GOOD, alpha=0.0 for ANALYST_NEUTRAL
    factor_cols = ["Mkt-RF", "SMB", "HML", "MOM"]
    X = factor_panel[factor_cols].values
    true_beta_good = np.array([0.0, 0.1, -0.3, -0.2])
    true_beta_neutral = np.array([0.0, 0.0, 0.0, 0.0])
    
    rng = np.random.default_rng(42)
    n_months = len(factor_panel)
    
    # Create quarterly panel (forward-fill monthly to quarterly)
    quarters = pd.period_range("2015Q1", periods=n_months // 3, freq="Q")
    quarterly_idx = quarters.to_timestamp(how="end")
    
    rows = []
    for analyst, true_alpha, true_beta in [
        ("ANALYST_GOOD", 0.62, true_beta_good),
        ("ANALYST_NEUTRAL", 0.0, true_beta_neutral),
    ]:
        residuals = true_alpha + X @ true_beta + rng.normal(0, 1.0, n_months)
        # Convert to quarterly by taking last month of each quarter
        for i, q_idx in enumerate(quarterly_idx):
            if i * 3 < n_months:
                monthly_idx = i * 3 + 2  # last month of quarter
                rows.append({
                    "analyst": analyst,
                    "firm": "FIRM1",
                    "quarter": f"{q_idx.year}Q{q_idx.quarter}",
                    "fe": residuals[monthly_idx] + 0.001,  # fe = residual + tiny predicted
                    "nn_predicted_fe": 0.001,
                })
    
    panel = pd.DataFrame(rows)
    result = compute_analyst_factor_alpha(panel, factor_model="FF3+MOM", min_obs=60)
    
    # Both analysts should have valid results (>=60 monthly obs via forward-fill)
    assert result.loc["ANALYST_GOOD", "n_obs"] >= 60
    assert result.loc["ANALYST_NEUTRAL", "n_obs"] >= 60
    
    # ANALYST_GOOD should recover alpha near 0.62
    good_alpha = result.loc["ANALYST_GOOD", "factor_alpha"]
    assert abs(good_alpha - 0.62) < 0.15, f"Expected ~0.62, got {good_alpha}"
    
    # ANALYST_NEUTRAL should recover alpha near 0.0
    neutral_alpha = result.loc["ANALYST_NEUTRAL", "factor_alpha"]
    assert abs(neutral_alpha - 0.0) < 0.15, f"Expected ~0.0, got {neutral_alpha}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd analyst-model
pytest tests/test_factor_adjusted_scoring.py::test_compute_analyst_factor_alpha_insufficient_obs -v
# Expected: FAIL - function not defined
```

- [ ] **Step 3: Implement compute_analyst_factor_alpha() in factors.py**

```python
# src/factors.py - add after selfcheck() function, before if __name__ == "__main__":

def compute_analyst_factor_alpha(
    analyst_fe_panel: pd.DataFrame,
    factor_model: str = FACTOR_MODEL,
    min_obs: int = MIN_FACTOR_OBS,
    use_cache: bool = True,
    download_missing: bool = False,
) -> pd.DataFrame:
    """
    Compute per-analyst factor-model alpha on forecast-error residuals.

    Parameters
    ----------
    analyst_fe_panel : DataFrame
        Columns ['analyst', 'firm', 'quarter', 'fe', 'nn_predicted_fe'].
        One row per (analyst, firm, quarter).
    factor_model : str
        'FF3+MOM' or 'FF5+MOM' (passed to load_factors).
    min_obs : int
        Minimum monthly observations required for regression (default 60).
        Quarterly data is forward-filled to monthly within each quarter.
    use_cache, download_missing : bool
        Passed to load_factors().

    Returns
    -------
    DataFrame indexed by analyst with columns:
    ['factor_alpha', 'factor_alpha_tstat', 'factor_alpha_annualized',
     'loading_Mkt-RF', 'loading_SMB', 'loading_HML', 'loading_MOM',
     (+ 'loading_RMW', 'loading_CMA' for FF5+MOM),
     'n_obs', 'r_squared']
    Analysts with < min_obs get all-NaN rows (index preserved for alignment).
    """
    from src.config import MIN_FACTOR_OBS as DEFAULT_MIN_OBS
    min_obs = min_obs or DEFAULT_MIN_OBS
    
    if analyst_fe_panel.empty:
        return pd.DataFrame(columns=[
            "factor_alpha", "factor_alpha_tstat", "factor_alpha_annualized",
            "loading_Mkt-RF", "loading_SMB", "loading_HML", "loading_MOM",
            "loading_RMW", "loading_CMA", "n_obs", "r_squared"
        ]).astype(float)
    
    # 1. Compute residuals = fe - nn_predicted_fe (paper's "sentiment")
    panel = analyst_fe_panel.copy()
    panel["residual"] = panel["fe"] - panel["nn_predicted_fe"]
    
    # 2. Convert quarterly to monthly via forward-fill within quarter
    # Quarter label like "2020Q1" -> month-ends Jan, Feb, Mar all get same residual
    def quarter_to_month_ends(q_str: str) -> list[pd.Timestamp]:
        year = int(q_str[:4])
        quarter = int(q_str[5])
        start_month = (quarter - 1) * 3 + 1
        return [
            pd.Timestamp(year, m, 1) + pd.offsets.MonthEnd(0)
            for m in range(start_month, start_month + 3)
        ]
    
    monthly_rows = []
    for _, row in panel.iterrows():
        for dt in quarter_to_month_ends(row["quarter"]):
            monthly_rows.append({
                "analyst": row["analyst"],
                "date": dt,
                "residual": row["residual"],
            })
    
    monthly = pd.DataFrame(monthly_rows)
    if monthly.empty:
        return pd.DataFrame()  # no data
    
    # 3. Load factor panel
    factor_model_obj = load_factors(
        model=factor_model,
        use_cache=use_cache,
        download_missing=download_missing,
    )
    factor_panel = factor_model_obj.factors  # monthly, DatetimeIndex, %/mo
    
    # 4. Per-analyst regression
    results = []
    factor_cols = [c for c in ("Mkt-RF", "SMB", "HML", "MOM", "RMW", "CMA") if c in factor_panel.columns]
    
    for analyst, g in monthly.groupby("analyst"):
        # Align residual series with factor panel on date
        g = g.set_index("date")["residual"]
        df = pd.concat([g.rename("y"), factor_panel[factor_cols]], axis=1, join="inner").dropna()
        
        n = len(df)
        if n < min_obs:
            # Insufficient observations - return NaN row with n_obs
            results.append({
                "analyst": analyst,
                "factor_alpha": np.nan,
                "factor_alpha_tstat": np.nan,
                "factor_alpha_annualized": np.nan,
                **{f"loading_{c}": np.nan for c in factor_cols},
                "n_obs": n,
                "r_squared": np.nan,
            })
            continue
        
        y = df["y"].values
        X = df[factor_cols].values
        k = len(factor_cols)
        if n <= k + 1:
            results.append({
                "analyst": analyst,
                "factor_alpha": np.nan,
                "factor_alpha_tstat": np.nan,
                "factor_alpha_annualized": np.nan,
                **{f"loading_{c}": np.nan for c in factor_cols},
                "n_obs": n,
                "r_squared": np.nan,
            })
            continue
        
        # OLS (reuse regress_on_factors logic but inline for per-analyst)
        Xd = np.column_stack([np.ones(n), X])
        beta, *_ = np.linalg.lstsq(Xd, y, rcond=None)
        resid = y - Xd @ beta
        dof = n - (k + 1)
        sigma2 = resid @ resid / dof
        cov = sigma2 * np.linalg.pinv(Xd.T @ Xd)
        se = np.sqrt(np.diag(cov))
        tstats = beta / se
        
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r_squared = 1.0 - float(resid @ resid) / ss_tot if ss_tot > 0 else np.nan
        
        results.append({
            "analyst": analyst,
            "factor_alpha": float(beta[0]),
            "factor_alpha_tstat": float(tstats[0]),
            "factor_alpha_annualized": float(beta[0]) * 12.0,
            **{f"loading_{c}": float(b) for c, b in zip(factor_cols, beta[1:])},
            "n_obs": n,
            "r_squared": r_squared,
        })
    
    result_df = pd.DataFrame(results).set_index("analyst")
    
    # Ensure all factor columns exist (FF3 vs FF5)
    for c in ("RMW", "CMA"):
        col = f"loading_{c}"
        if col not in result_df.columns:
            result_df[col] = np.nan
    
    # Reorder columns
    base_cols = ["factor_alpha", "factor_alpha_tstat", "factor_alpha_annualized"]
    loading_cols = [f"loading_{c}" for c in ("Mkt-RF", "SMB", "HML", "MOM", "RMW", "CMA")]
    tail_cols = ["n_obs", "r_squared"]
    result_df = result_df[base_cols + loading_cols + tail_cols]
    
    return result_df
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd analyst-model
pytest tests/test_factor_adjusted_scoring.py -v
# Expected: PASS both tests
```

- [ ] **Step 5: Run factors.py selfcheck to ensure no regression**

```bash
cd analyst-model
python src/factors.py
# Expected: "OVERALL: PASS"
```

- [ ] **Step 6: Commit**

```bash
git add src/factors.py tests/test_factor_adjusted_scoring.py
git commit -m "factors: add compute_analyst_factor_alpha() with per-analyst factor regression"
```

---

### Task 3: Add compute_factor_adjusted_scores() and CLI flag to master_pipeline.py

**Files:**
- Modify: `master_pipeline.py` (add function after `simple_accuracy_leaderboard()`, add CLI arg, wire into `main()`)

**Interfaces:**
- Consumes: `predicted` DataFrame (output of `run_expanding_window()`), `factor_model` string
- Produces: DataFrame with all columns from `analyst_reliability_scores()` PLUS:
  `['factor_alpha', 'factor_alpha_tstat', 'factor_alpha_annualized', 'factor_alpha_z',
   'loading_Mkt-RF', 'loading_SMB', 'loading_HML', 'loading_MOM',
   'loading_RMW', 'loading_CMA', 'factor_adjusted_composite']`
- Analysts with insufficient factor data have NaN factor columns; `factor_alpha_z` excluded from z-mean automatically

- [ ] **Step 1: Write failing test**

```python
# tests/test_factor_adjusted_scoring.py - add to existing file
def test_compute_factor_adjusted_scores_augments_existing():
    """New function adds factor columns without changing original scores."""
    from master_pipeline import compute_factor_adjusted_scores, _make_mock_data, run_expanding_window
    
    raw_fe = _make_mock_data(n_analysts=4, n_firms=2, n_years=20, seed=123)  # 20 years = 80 quarters > 60 monthly
    predicted = run_expanding_window(raw_fe)
    
    scores = compute_factor_adjusted_scores(predicted, factor_model="FF3+MOM")
    
    # Should have all original columns
    original_cols = {"analyst", "n_predictions", "accuracy_score", "predictability_r2",
                     "consistency_score", "reliability_composite_raw", "credibility_weight",
                     "reliability_composite", "current_broker", "current_broker_code", "n_brokers"}
    assert original_cols.issubset(set(scores.columns)), f"Missing: {original_cols - set(scores.columns)}"
    
    # Should have new factor columns
    factor_cols = {"factor_alpha", "factor_alpha_tstat", "factor_alpha_annualized",
                   "factor_alpha_z", "loading_Mkt-RF", "loading_SMB", "loading_HML",
                   "loading_MOM", "loading_RMW", "loading_CMA", "factor_adjusted_composite"}
    assert factor_cols.issubset(set(scores.columns)), f"Missing: {factor_cols - set(scores.columns)}"
    
    # reliability_composite should be unchanged (compare to direct call)
    from master_pipeline import analyst_reliability_scores
    original_scores = analyst_reliability_scores(predicted)
    pd.testing.assert_series_equal(
        scores["reliability_composite"].sort_index(),
        original_scores["reliability_composite"].sort_index(),
        check_names=False,
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd analyst-model
pytest tests/test_factor_adjusted_scoring.py::test_compute_factor_adjusted_scores_augments_existing -v
# Expected: FAIL - function not defined
```

- [ ] **Step 3: Implement compute_factor_adjusted_scores() in master_pipeline.py**

```python
# master_pipeline.py - add after simple_accuracy_leaderboard() (around line 691)

def compute_factor_adjusted_scores(
    predicted: pd.DataFrame,
    factor_model: str = FACTOR_MODEL,
    min_factor_obs: int = MIN_FACTOR_OBS,
) -> pd.DataFrame:
    """
    Compute factor-adjusted analyst scores by augmenting analyst_reliability_scores()
    with per-analyst factor-model alpha on forecast-error residuals.

    Returns the full scores DataFrame with additional columns:
    - factor_alpha, factor_alpha_tstat, factor_alpha_annualized
    - factor_alpha_z (z-scored across analysts with valid alphas)
    - loading_Mkt-RF, loading_SMB, loading_HML, loading_MOM, loading_RMW, loading_CMA
    - factor_adjusted_composite = mean(z_source_cols) * credibility_weight
      where z_source_cols includes factor_alpha_z when available.
    """
    from src.factors import compute_analyst_factor_alpha
    from src.config import CREDIBILITY_PRIOR_OBS
    
    # 1. Get base scores (unchanged)
    base_scores = analyst_reliability_scores(predicted)
    if base_scores.empty:
        return base_scores
    
    # 2. Compute factor alphas on the predicted panel
    # predicted has: analyst, firm, quarter, year, fe, nn_predicted_fe
    factor_results = compute_analyst_factor_alpha(
        predicted[["analyst", "firm", "quarter", "fe", "nn_predicted_fe"]],
        factor_model=factor_model,
        min_obs=min_factor_obs,
    )
    
    if factor_results.empty:
        # No factor results - return base scores with NaN factor columns
        factor_cols = ["factor_alpha", "factor_alpha_tstat", "factor_alpha_annualized",
                       "factor_alpha_z", "loading_Mkt-RF", "loading_SMB", "loading_HML",
                       "loading_MOM", "loading_RMW", "loading_CMA"]
        for c in factor_cols:
            base_scores[c] = np.nan
        base_scores["factor_adjusted_composite"] = base_scores["reliability_composite"]
        return base_scores
    
    # 3. Merge factor results into base scores
    # factor_results is indexed by analyst
    merged = base_scores.set_index("analyst").join(factor_results, how="left").reset_index()
    
    # 4. Z-score factor_alpha (only for analysts with valid alpha)
    if "factor_alpha" in merged.columns:
        valid_mask = merged["factor_alpha"].notna()
        if valid_mask.any():
            mu = merged.loc[valid_mask, "factor_alpha"].mean()
            sigma = merged.loc[valid_mask, "factor_alpha"].std()
            if sigma and sigma > 0:
                merged["factor_alpha_z"] = np.nan
                merged.loc[valid_mask, "factor_alpha_z"] = (
                    (merged.loc[valid_mask, "factor_alpha"] - mu) / sigma
                )
            else:
                merged["factor_alpha_z"] = 0.0
        else:
            merged["factor_alpha_z"] = np.nan
    
    # 5. Build factor_adjusted_composite
    # Same z-source cols as reliability_composite, plus factor_alpha_z when available
    z_source_cols = ["accuracy_score_z", "predictability_r2_z", "consistency_score_z"]
    if "freshness_score_z" in merged.columns:
        z_source_cols.append("freshness_score_z")
    if "factor_alpha_z" in merged.columns and merged["factor_alpha_z"].notna().any():
        z_source_cols.append("factor_alpha_z")
    
    # Mean across available z-scores per row (skips NaN automatically)
    merged["factor_adjusted_composite_raw"] = merged[z_source_cols].mean(axis=1)
    
    # Apply credibility weighting (same formula)
    merged["credibility_weight"] = merged["n_predictions"] / (merged["n_predictions"] + CREDIBILITY_PRIOR_OBS)
    merged["factor_adjusted_composite"] = merged["factor_adjusted_composite_raw"] * merged["credibility_weight"]
    
    # 6. Sort by factor_adjusted_composite descending
    merged = merged.sort_values("factor_adjusted_composite", ascending=False).reset_index(drop=True)
    
    return merged
```

- [ ] **Step 4: Add CLI flag and wire into main()**

```python
# master_pipeline.py - in main(), add to argument parser (around line 1065)

# Section 7 additions: Factor-adjusted analyst scoring
parser.add_argument(
    "--factor-adjusted", action="store_true",
    help="Compute factor-adjusted analyst scores (adds factor-alpha component "
         "to reliability composite). Uses --factor-model. Requires sufficient "
         "analyst history (default 60 monthly obs ~ 5 years). "
         "Writes outputs/live_<TICKER>_factor_adjusted_scores.csv.",
)
```

```python
# master_pipeline.py - in main(), after run_pipeline() call (around line 1120)
# Add factor-adjusted scoring if requested

if args.factor_adjusted:
    print(f"\n[FA] Computing factor-adjusted analyst scores (model={args.factor_model})...")
    factor_adjusted_scores = compute_factor_adjusted_scores(
        results["predicted"],
        factor_model=args.factor_model,
        min_factor_obs=MIN_FACTOR_OBS,
    )
    
    # Check how many analysts got valid factor alphas
    n_valid = factor_adjusted_scores["factor_alpha"].notna().sum()
    n_total = len(factor_adjusted_scores)
    print(f"    {n_valid}/{n_total} analysts have sufficient history for factor-alpha")
    
    if n_valid == 0:
        print("    WARNING: No analysts met MIN_FACTOR_OBS threshold. "
              "Factor columns will be NaN. This is expected for single-ticker runs.")
    
    # Write new CSV
    os.makedirs("outputs", exist_ok=True)
    safe_ticker = args.ticker.replace("-", "_") if args.ticker else "mock"
    factor_adjusted_scores.to_csv(
        f"outputs/live_{safe_ticker}_factor_adjusted_scores.csv", index=False
    )
    print(f"    Wrote outputs/live_{safe_ticker}_factor_adjusted_scores.csv")
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd analyst-model
pytest tests/test_factor_adjusted_scoring.py -v
# Expected: PASS all tests
```

- [ ] **Step 6: Test mock run with --factor-adjusted**

```bash
cd analyst-model
python master_pipeline.py --mock --factor-adjusted
# Expected: Runs without error, writes mock_factor_adjusted_scores.csv,
# prints warning about insufficient obs, existing outputs unchanged
```

- [ ] **Step 7: Commit**

```bash
git add master_pipeline.py
git commit -m "pipeline: add compute_factor_adjusted_scores() and --factor-adjusted CLI flag"
```

---

### Task 4: Create export_factor_adjusted_excel.py

**Files:**
- Create: `export_factor_adjusted_excel.py` (new file, mirrors `export_to_excel.py`)
- Reference: `export_to_excel.py` for structure

**Interfaces:**
- Consumes: `outputs/live_<TICKER>_factor_adjusted_scores.csv`, `outputs/live_<TICKER>_raw_forecast_errors.csv`, `outputs/live_<TICKER>_run_info.csv`
- Produces: `outputs/live_<TICKER>_factor_adjusted_model.xlsx` with sheets:
  - `Run Info` (same as current)
  - `Raw Data` (same as current)
  - `Leaderboard` (adds `Factor Alpha (%/mo)`, `Factor Alpha t-stat`, `Factor Adj Score` columns with Excel formulas referencing `Raw Data` + factor regression results)
  - `FactorAlpha` (new sheet: per-analyst regression table with alpha, t-stat, loadings, R², n_obs)
  - `Quarterly` (same as current)

- [ ] **Step 1: Read export_to_excel.py to understand structure**

```bash
cd analyst-model
cat export_to_excel.py
# Note: creates workbook with formulas, charts, specific column structure
```

- [ ] **Step 2: Create export_factor_adjusted_excel.py**

```python
#!/usr/bin/env python3
"""
export_factor_adjusted_excel.py
================================
Builds an Excel workbook (.xlsx) from a --live --factor-adjusted run's
output CSVs. Mirrors export_to_excel.py but adds:
- FactorAlpha sheet with per-analyst factor regression results
- Leaderboard sheet adds Factor Alpha and Factor Adj Score columns

Usage:
    python export_factor_adjusted_excel.py --ticker AAPL-US
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Font, PatternFill, Alignment, numbers
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.dirname(__file__))
from src.config import CREDIBILITY_PRIOR_OBS


def build_workbook(ticker: str) -> tuple[Workbook, dict]:
    """Build the workbook, return (wb, paths_dict)."""
    safe = ticker.replace("-", "_")
    base = Path("outputs")
    
    paths = {
        "scores": base / f"live_{safe}_factor_adjusted_scores.csv",
        "raw": base / f"live_{safe}_raw_forecast_errors.csv",
        "run_info": base / f"live_{safe}_run_info.csv",
        "consensus": base / f"live_{safe}_consensus.csv",
        "industry": base / f"live_{safe}_industry_sentiment.csv",
    }
    
    # Verify required files exist
    for name, p in paths.items():
        if not p.exists():
            raise FileNotFoundError(f"Missing {name}: {p}. Run master_pipeline.py --live --ticker {ticker} --factor-adjusted first.")
    
    scores = pd.read_csv(paths["scores"])
    raw = pd.read_csv(paths["raw"])
    run_info = pd.read_csv(paths["run_info"])
    consensus = pd.read_csv(paths["consensus"]) if paths["consensus"].exists() else pd.DataFrame()
    industry = pd.read_csv(paths["industry"]) if paths["industry"].exists() else pd.DataFrame()
    
    wb = Workbook()
    
    # ===== Styles =====
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    title_font = Font(bold=True, size=14, color="2F5496")
    pct_fmt = '0.00%'
    num_fmt = '#,##0.0000'
    int_fmt = '#,##0'
    
    def style_header_row(ws, row=1):
        for cell in ws[row]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
    
    def auto_width(ws, min_width=10, max_width=40):
        for col in ws.columns:
            max_len = min(max(len(str(c.value or "")) for c in col) + 2, max_width)
            ws.column_dimensions[get_column_letter(col[0].column)].width = max(max_len, min_width)
    
    # ===== Sheet 1: Run Info =====
    ws_info = wb.active
    ws_info.title = "Run Info"
    ws_info["A1"] = f"Factor-Adjusted Analyst Model Run — {ticker}"
    ws_info["A1"].font = title_font
    
    for i, (_, row) in enumerate(run_info.iterrows(), start=3):
        for j, (col, val) in enumerate(row.items(), start=1):
            cell = ws_info.cell(row=i, column=j, value=col if i == 3 else val)
            if i == 3:
                cell.font = header_font
                cell.fill = header_fill
    
    auto_width(ws_info)
    
    # ===== Sheet 2: Raw Data =====
    ws_raw = wb.create_sheet("Raw Data")
    for j, col in enumerate(raw.columns, 1):
        ws_raw.cell(row=1, column=j, value=col)
    for i, (_, row) in enumerate(raw.iterrows(), 2):
        for j, val in enumerate(row, 1):
            ws_raw.cell(row=i, column=j, value=val)
    style_header_row(ws_raw)
    auto_width(ws_raw)
    
    # ===== Sheet 3: Leaderboard (with Factor Alpha + Factor Adj Score) =====
    ws_lb = wb.create_sheet("Leaderboard")
    
    # Select and order columns for leaderboard
    lb_cols = [
        "analyst", "current_broker", "current_broker_code", "n_brokers",
        "n_predictions", "credibility_weight",
        "accuracy_score", "accuracy_score_z",
        "predictability_r2", "predictability_r2_z",
        "consistency_score", "consistency_score_z",
    ]
    if "freshness_score" in scores.columns:
        lb_cols += ["freshness_score", "freshness_score_z"]
    if "avg_staleness_days" in scores.columns:
        lb_cols.append("avg_staleness_days")
    
    # NEW: Factor columns
    lb_cols += [
        "factor_alpha", "factor_alpha_tstat", "factor_alpha_z",
        "loading_Mkt-RF", "loading_SMB", "loading_HML", "loading_MOM",
        "loading_RMW", "loading_CMA",
        "reliability_composite_raw", "reliability_composite",
        "factor_adjusted_composite_raw", "factor_adjusted_composite",
    ]
    
    # Filter to columns that exist
    lb_cols = [c for c in lb_cols if c in scores.columns]
    
    lb_df = scores[lb_cols].copy()
    
    # Write headers
    for j, col in enumerate(lb_df.columns, 1):
        ws_lb.cell(row=1, column=j, value=col)
    
    # Write data with Excel formulas where possible
    # For factor columns, we write values (they come from regression, not raw data formulas)
    for i, (_, row) in enumerate(lb_df.iterrows(), 2):
        for j, col in enumerate(lb_df.columns, 1):
            val = row[col]
            cell = ws_lb.cell(row=i, column=j, value=val)
            # Format percentages
            if "score" in col.lower() or "alpha" in col.lower() or "loading" in col.lower() or "r2" in col.lower() or "consistency" in col.lower():
                if isinstance(val, float) and not pd.isna(val):
                    cell.number_format = num_fmt
            elif "predictability" in col.lower() or "weight" in col.lower() or "composite" in col.lower():
                if isinstance(val, float) and not pd.isna(val):
                    cell.number_format = pct_fmt
    
    style_header_row(ws_lb)
    auto_width(ws_lb)
    
    # Add bar chart for factor_adjusted_composite (top 15)
    chart = BarChart()
    chart.type = "col"
    chart.style = 10
    chart.title = f"Factor-Adjusted Reliability Composite — {ticker} (Top 15)"
    chart.y_axis.title = "Factor Adjusted Composite"
    chart.x_axis.title = "Analyst"
    
    n_plot = min(15, len(lb_df))
    data = Reference(ws_lb, min_col=lb_df.columns.get_loc("factor_adjusted_composite") + 1,
                     min_row=1, max_row=n_plot + 1)
    cats = Reference(ws_lb, min_col=lb_df.columns.get_loc("analyst") + 1,
                     min_row=2, max_row=n_plot + 1)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    chart.shape = 4
    ws_lb.add_chart(chart, "A" + str(len(lb_df) + 5))
    
    # ===== Sheet 4: FactorAlpha (NEW) =====
    ws_fa = wb.create_sheet("FactorAlpha")
    
    fa_cols = [
        "analyst", "factor_alpha", "factor_alpha_tstat", "factor_alpha_annualized",
        "loading_Mkt-RF", "loading_SMB", "loading_HML", "loading_MOM",
        "loading_RMW", "loading_CMA",
        "n_obs", "r_squared",
    ]
    fa_cols = [c for c in fa_cols if c in scores.columns]
    
    fa_df = scores[fa_cols].copy().sort_values("factor_alpha", ascending=False, na_position="last")
    
    for j, col in enumerate(fa_df.columns, 1):
        ws_fa.cell(row=1, column=j, value=col)
    
    for i, (_, row) in enumerate(fa_df.iterrows(), 2):
        for j, col in enumerate(fa_df.columns, 1):
            val = row[col]
            cell = ws_fa.cell(row=i, column=j, value=val)
            if "alpha" in col.lower() or "loading" in col.lower():
                if isinstance(val, float) and not pd.isna(val):
                    cell.number_format = num_fmt
            elif col == "r_squared":
                if isinstance(val, float) and not pd.isna(val):
                    cell.number_format = '0.00%'
            elif col == "n_obs":
                if isinstance(val, (int, float)) and not pd.isna(val):
                    cell.number_format = int_fmt
    
    style_header_row(ws_fa)
    auto_width(ws_fa)
    
    # ===== Sheet 5: Quarterly (Consensus FE over time) =====
    if not consensus.empty:
        ws_q = wb.create_sheet("Quarterly")
        
        # Pivot: quarter x (consensus_fe, consensus_nn_predicted_fe)
        q_pivot = consensus.pivot_table(
            index="quarter",
            values=["consensus_fe", "consensus_nn_predicted_fe"],
            aggfunc="first",
        ).sort_index()
        
        for j, col in enumerate(["quarter"] + list(q_pivot.columns), 1):
            ws_q.cell(row=1, column=j, value=col)
        
        for i, (qtr, row) in enumerate(q_pivot.iterrows(), 2):
            ws_q.cell(row=i, column=1, value=qtr)
            for j, val in enumerate(row, 2):
                cell = ws_q.cell(row=i, column=j, value=val)
                if isinstance(val, float) and not pd.isna(val):
                    cell.number_format = num_fmt
        
        style_header_row(ws_q)
        auto_width(ws_q)
        
        # Line chart
        chart2 = LineChart()
        chart2.title = f"Consensus Forecast Error — {ticker}"
        chart2.y_axis.title = "Forecast Error"
        chart2.x_axis.title = "Quarter"
        chart2.style = 10
        
        n_q = len(q_pivot)
        data2 = Reference(ws_q, min_col=2, max_col=3, min_row=1, max_row=n_q + 1)
        cats2 = Reference(ws_q, min_col=1, min_row=2, max_row=n_q + 1)
        chart2.add_data(data2, titles_from_data=True)
        chart2.set_categories(cats2)
        ws_q.add_chart(chart2, "A" + str(n_q + 5))
    
    # ===== Sheet 6: Industry Sentiment (if available) =====
    if not industry.empty:
        ws_ind = wb.create_sheet("Industry Sentiment")
        for j, col in enumerate(industry.columns, 1):
            ws_ind.cell(row=1, column=j, value=col)
        for i, (_, row) in enumerate(industry.iterrows(), 2):
            for j, val in enumerate(row, 1):
                cell = ws_ind.cell(row=i, column=j, value=val)
                if "sentiment" in str(industry.columns[j-1]).lower() or "fe" in str(industry.columns[j-1]).lower():
                    if isinstance(val, float) and not pd.isna(val):
                        cell.number_format = num_fmt
        style_header_row(ws_ind)
        auto_width(ws_ind)
    
    return wb, paths


def main():
    parser = argparse.ArgumentParser(description="Build factor-adjusted Excel workbook from --live --factor-adjusted run")
    parser.add_argument("--ticker", required=True, help="Ticker symbol, e.g. AAPL-US")
    args = parser.parse_args()
    
    print(f"Building factor-adjusted workbook for {args.ticker}...")
    wb, paths = build_workbook(args.ticker)
    
    safe = args.ticker.replace("-", "_")
    out_path = Path("outputs") / f"live_{safe}_factor_adjusted_model.xlsx"
    wb.save(out_path)
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Test the exporter**

```bash
cd analyst-model
# First run mock with factor-adjusted to generate CSVs
python master_pipeline.py --mock --factor-adjusted

# Then run exporter
python export_factor_adjusted_excel.py --ticker MOCK_TICKER
# Note: mock run writes to outputs/mock_* not live_*, so adjust or check actual output names

# Check file exists and has expected sheets
python -c "
import openpyxl
wb = openpyxl.load_workbook('outputs/mock_factor_adjusted_model.xlsx')
print('Sheets:', wb.sheetnames)
for s in wb.sheetnames:
    ws = wb[s]
    print(f'  {s}: {ws.max_row} rows x {ws.max_column} cols')
"
# Expected: Sheets include 'Run Info', 'Raw Data', 'Leaderboard', 'FactorAlpha', 'Quarterly', 'Industry Sentiment'
```

- [ ] **Step 4: Commit**

```bash
git add export_factor_adjusted_excel.py
git commit -m "excel: add export_factor_adjusted_excel.py for factor-adjusted scoring workbook"
```

---

### Task 5: Integration Test & Documentation

**Files:**
- Test: Full mock run with `--factor-adjusted` + exporter
- Docs: Update README.md with new flag and exporter

- [ ] **Step 1: Full integration test**

```bash
cd analyst-model

# Clean outputs
rm -rf outputs
mkdir outputs

# Mock run with factor-adjusted
python master_pipeline.py --mock --factor-adjusted --factor-model FF3+MOM
# Verify: mock_factor_adjusted_scores.csv exists, mock_analyst_scores.csv unchanged

# Check CSVs
python -c "
import pandas as pd
orig = pd.read_csv('outputs/mock_analyst_scores.csv')
new = pd.read_csv('outputs/mock_factor_adjusted_scores.csv')
print('Original cols:', list(orig.columns))
print('New cols:', list(new.columns))
print('Original composite match:', orig['reliability_composite'].tolist() == new['reliability_composite'].tolist())
print('New has factor_alpha:', 'factor_alpha' in new.columns)
print('New has factor_adjusted_composite:', 'factor_adjusted_composite' in new.columns)
print('Factor alphas:', new['factor_alpha'].tolist())
"

# Expected: factor_alpha all NaN (mock has < 60 monthly obs), factor_adjusted_composite == reliability_composite
```

- [ ] **Step 2: Test exporter on mock outputs**

```bash
cd analyst-model
# Mock writes to mock_* not live_*, so test with a live-like ticker name
# Or temporarily modify exporter to accept mock prefix
# For now, just verify the script loads without error
python -c "import export_factor_adjusted_excel; print('Import OK')"
```

- [ ] **Step 3: Update README.md**

```markdown
# In README.md, add to the running section:

## Factor-Adjusted Analyst Scoring (New)

```bash
# Run with factor-alpha augmentation (adds Fama-French risk adjustment to analyst scores)
python master_pipeline.py --live --ticker AAPL-US --factor-adjusted

# Build factor-adjusted Excel workbook (separate from standard workbook)
python export_factor_adjusted_excel.py --ticker AAPL-US
```

**Outputs:**
- `outputs/live_<TICKER>_factor_adjusted_scores.csv` — adds `factor_alpha`, `factor_alpha_tstat`, `factor_adjusted_composite`, loadings
- `outputs/live_<TICKER>_factor_adjusted_model.xlsx` — workbook with `FactorAlpha` sheet + augmented `Leaderboard`

**Note:** Factor-alpha requires ~60 monthly observations per analyst (≈5 years, multi-ticker universe). Single-ticker runs will show NaN factor columns with a warning — this is expected.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: add --factor-adjusted flag and export_factor_adjusted_excel.py to README"
```

---

### Task 6: Verify No Regressions

**Files:**
- Run: Full test suite (if any), selfcheck, mock runs

- [ ] **Step 1: Run factors.py selfcheck**

```bash
cd analyst-model
python src/factors.py
# Expected: "OVERALL: PASS"
```

- [ ] **Step 2: Run mock without --factor-adjusted (baseline)**

```bash
cd analyst-model
rm -rf outputs && mkdir outputs
python master_pipeline.py --mock
# Verify outputs/mock_analyst_scores.csv exists and matches pre-change baseline
```

- [ ] **Step 3: Run mock with --factor-adjusted**

```bash
cd analyst-model
rm -rf outputs && mkdir outputs
python master_pipeline.py --mock --factor-adjusted
# Verify both CSVs exist, original unchanged
```

- [ ] **Step 4: Run live single-ticker (if credentials available) — optional manual verification**

```bash
# Only if user has FactSet credentials configured
# python master_pipeline.py --live --ticker AAPL-US --factor-adjusted
# Verify warning about insufficient obs, new CSV created
```

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "test: verify no regressions in factor-adjusted scoring"
```

---

## Summary of Deliverables

| Task | Deliverable |
|------|-------------|
| 1 | `src/config.py` + `MIN_FACTOR_OBS = 60` |
| 2 | `src/factors.py` + `compute_analyst_factor_alpha()` + tests |
| 3 | `master_pipeline.py` + `compute_factor_adjusted_scores()` + `--factor-adjusted` CLI |
| 4 | `export_factor_adjusted_excel.py` (new) |
| 5 | README.md updated, integration tests passing |
| 6 | Regression verification complete |

---

## Dependencies Between Tasks

```
Task 1 (config) → Task 2 (factors) → Task 3 (pipeline) → Task 4 (excel) → Task 5 (integration) → Task 6 (regression)
                         ↘
                          → Task 2 tests use MIN_FACTOR_OBS from Task 1
```

Each task produces independently testable output. Task 2 tests can run after Task 1. Task 3 tests need Task 2. Task 4 is independent after Task 3. Task 5 needs all prior.