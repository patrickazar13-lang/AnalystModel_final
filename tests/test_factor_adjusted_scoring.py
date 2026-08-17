"""
Tests for factor-adjusted analyst scoring.

Run with: PYTHONPATH=. pytest tests/test_factor_adjusted_scoring.py -v
"""
import numpy as np
import pandas as pd
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.factors import compute_analyst_factor_alpha, load_factors
from src.config import MIN_FACTOR_OBS


def test_compute_analyst_factor_alpha_insufficient_obs():
    """Analysts with < MIN_FACTOR_OBS get NaN rows, index preserved."""
    # Build panel with 2 analysts, only 8 quarters = 24 monthly obs (less than MIN_FACTOR_OBS=60)
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
    # All factor columns should be NaN (24 monthly obs < 60 min_obs)
    assert result["factor_alpha"].isna().all()
    # n_obs should be 24 (8 quarters * 3 months each)
    assert result["n_obs"].eq(24).all()
    print("test_compute_analyst_factor_alpha_insufficient_obs PASSED")


def test_compute_analyst_factor_alpha_recovers_injected_alpha():
    """Synthetic panel with known factor structure recovers injected alpha."""
    # Build synthetic factor panel matching _synthetic_factor_panel in factors.py
    # Use enough months to get >= 60 monthly obs after forward-fill (20 quarters = 60 months)
    rng = np.random.default_rng(0)
    n_months = 120  # 10 years, 40 quarters
    idx = pd.date_range("2015-01-31", periods=n_months, freq="ME")
    factor_panel = pd.DataFrame({
        "Mkt-RF": rng.normal(0.5, 4.0, n_months),
        "SMB": rng.normal(0.2, 2.5, n_months),
        "HML": rng.normal(0.2, 3.0, n_months),
        "MOM": rng.normal(0.3, 4.0, n_months),
        "RF": np.full(n_months, 0.15),
    }, index=idx)

    # Build analyst residuals = alpha + X @ beta + noise
    # We'll inject alpha=0.62 for ANALYST_GOOD, alpha=0.0 for ANALYST_NEUTRAL
    factor_cols = ["Mkt-RF", "SMB", "HML", "MOM"]
    X = factor_panel[factor_cols].values
    true_beta_good = np.array([0.0, 0.1, -0.3, -0.2])
    true_beta_neutral = np.array([0.0, 0.0, 0.0, 0.0])

    rng = np.random.default_rng(42)

    # Create quarterly panel: one value per quarter (last month of quarter)
    # The function will forward-fill each quarterly value to 3 months
    quarters = pd.period_range("2015Q1", periods=n_months // 3, freq="Q")
    quarterly_idx = quarters.to_timestamp(how="end")

    rows = []
    for analyst, true_alpha, true_beta in [
        ("ANALYST_GOOD", 0.62, true_beta_good),
        ("ANALYST_NEUTRAL", 0.0, true_beta_neutral),
    ]:
        residuals_monthly = true_alpha + X @ true_beta + rng.normal(0, 1.0, n_months)
        # Use the LAST month of each quarter as the quarter's value
        # This matches how the paper's quarterly sentiment applies to all 3 months
        for i, q_idx in enumerate(quarterly_idx):
            monthly_idx = i * 3 + 2  # last month of quarter (0-indexed)
            if monthly_idx < n_months:
                quarter_residual = residuals_monthly[monthly_idx]
                rows.append({
                    "analyst": analyst,
                    "firm": "FIRM1",
                    "quarter": f"{q_idx.year}Q{q_idx.quarter}",
                    "fe": quarter_residual + 0.001,  # fe = residual + tiny predicted
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

    print("test_compute_analyst_factor_alpha_recovers_injected_alpha PASSED")


if __name__ == "__main__":
    # Can run directly without pytest
    test_compute_analyst_factor_alpha_insufficient_obs()
    test_compute_analyst_factor_alpha_recovers_injected_alpha()
    print("ALL TESTS PASSED")