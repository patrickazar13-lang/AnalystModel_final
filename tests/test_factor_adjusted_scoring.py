"""
Tests for factor-adjusted analyst scoring.

Run with: PYTHONPATH=. pytest tests/test_factor_adjusted_scoring.py -v

NOTE (updated): compute_analyst_factor_alpha() now regresses at the natural
QUARTERLY frequency instead of forward-filling each quarterly residual into
3 identical monthly copies. The forward-fill approach manufactured 3
fully-correlated, non-independent "observations" out of every 1 real one,
which understates the regression's true standard errors -- a real
statistical problem, not just a units difference -- and no analyst in this
project's actual data has 5 years (60 months) of quarterly coverage to ever
clear the old bar anyway. These tests were written against that old
behavior (asserting `n_obs >= 60` after forward-fill) and are rewritten here
to assert the corrected one (n_obs == number of real quarters, MIN_FACTOR_OBS
now defaults to 10 quarterly observations -- see config.py's comment for the
full rationale).
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
    """Analysts with < min_obs quarterly observations get NaN rows, index preserved."""
    # 8 real quarters, well under min_obs=10 -- should NOT get a factor_alpha.
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

    result = compute_analyst_factor_alpha(panel, factor_model="FF3+MOM", min_obs=10)

    # Both analysts should be in index
    assert list(result.index) == ["ANALYST_A", "ANALYST_B"]
    # All factor columns should be NaN (8 real quarterly obs < 10 min_obs)
    assert result["factor_alpha"].isna().all()
    # n_obs should be 8 -- one real, independent observation per quarter, not
    # 24 forward-filled monthly copies of the same 8 numbers.
    assert result["n_obs"].eq(8).all()
    print("test_compute_analyst_factor_alpha_insufficient_obs PASSED")


def test_compute_analyst_factor_alpha_recovers_injected_alpha():
    """Synthetic panel with known factor structure recovers injected alpha,
    regressed at the corrected quarterly frequency against real, cached
    Ken French quarterly-compounded factor returns (data/factors/*.csv --
    committed to the repo, 0 network calls needed to run this test)."""
    fm = load_factors(model="FF3+MOM", use_cache=True, download_missing=False)
    factors_q = (1.0 + fm.factors / 100.0).resample("QE").prod() * 100.0 - 100.0
    factor_cols = ["Mkt-RF", "SMB", "HML", "MOM"]

    # Last 24 real quarters of factor data -- plenty above min_obs=10, and
    # short enough to run fast / stay independent of how far back the cache goes.
    dates = factors_q.index[-24:]
    X = factors_q.loc[dates, factor_cols].values

    true_beta_good = np.array([0.0, 0.1, -0.3, -0.2])
    true_beta_neutral = np.array([0.0, 0.0, 0.0, 0.0])
    true_alpha_good = 0.62   # %/quarter (paper quotes a MONTHLY 0.62% -- this test
                              # only checks recovery, not the paper's specific units)
    true_alpha_neutral = 0.0

    rng = np.random.default_rng(42)
    rows = []
    for analyst, true_alpha, true_beta in [
        ("ANALYST_GOOD", true_alpha_good, true_beta_good),
        ("ANALYST_NEUTRAL", true_alpha_neutral, true_beta_neutral),
    ]:
        residuals = true_alpha + X @ true_beta + rng.normal(0, 1.0, len(dates))
        for dt, residual in zip(dates, residuals):
            rows.append({
                "analyst": analyst,
                "firm": "FIRM1",
                "quarter": f"{dt.year}Q{(dt.month - 1) // 3 + 1}",
                "fe": residual + 0.001,  # fe = residual + tiny predicted
                "nn_predicted_fe": 0.001,
            })

    panel = pd.DataFrame(rows)
    result = compute_analyst_factor_alpha(panel, factor_model="FF3+MOM", min_obs=10)

    assert result.loc["ANALYST_GOOD", "n_obs"] == len(dates)
    assert result.loc["ANALYST_NEUTRAL", "n_obs"] == len(dates)

    good_alpha = result.loc["ANALYST_GOOD", "factor_alpha"]
    assert abs(good_alpha - true_alpha_good) < 0.35, f"Expected ~{true_alpha_good}, got {good_alpha}"

    neutral_alpha = result.loc["ANALYST_NEUTRAL", "factor_alpha"]
    assert abs(neutral_alpha - true_alpha_neutral) < 0.35, f"Expected ~{true_alpha_neutral}, got {neutral_alpha}"

    print("test_compute_analyst_factor_alpha_recovers_injected_alpha PASSED")


if __name__ == "__main__":
    # Can run directly without pytest
    test_compute_analyst_factor_alpha_insufficient_obs()
    test_compute_analyst_factor_alpha_recovers_injected_alpha()
    print("ALL TESTS PASSED")
