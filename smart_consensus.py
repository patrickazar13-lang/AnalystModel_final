"""
Smart Consensus Model, Version 3

Purpose
-------
Compare ordinary Street consensus with a historically reliability weighted
"Smart Consensus" using the real master CSVs produced by master_pipeline.py.

This file makes ZERO FactSet/API calls.

Important design choice
-----------------------
The main project stores forecast error as a normalized error. The Smart
Consensus comparison therefore ALSO evaluates forecast accuracy using the
same normalized error basis:

    forecast_error = (forecast_EPS - actual_EPS) / price_10d_prior

This keeps the comparison consistent across companies with very different
EPS scales.

Out-of-sample rule
------------------
For a target firm-quarter, analyst weights are calculated using ONLY
observations strictly before that target quarter. No future observations are
allowed to influence the target prediction.

The first version uses:
    historical normalized MAE
    +
    credibility shrinkage

The current estimate is then weighted by that historical analyst weight.

Outputs
-------
outputs/smart_consensus_predictions.csv
outputs/smart_consensus_analyst_weights.csv
outputs/smart_consensus_summary.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


OUTPUT_DIR = Path("outputs")
DEFAULT_FORECAST_ERRORS = OUTPUT_DIR / "master_raw_forecast_errors.csv"
DEFAULT_ESTIMATES = OUTPUT_DIR / "master_raw_estimates.csv"

CREDIBILITY_PRIOR_OBS = 10
MIN_HISTORY_OBS = 4
MAE_FLOOR = 1e-6


def quarter_sort_key(q: str) -> tuple[int, int]:
    """Convert YYYYQn into a sortable tuple."""
    s = str(q).strip()
    try:
        return int(s[:4]), int(s[-1])
    except Exception:
        return 9999, 9


def add_quarter_order(df: pd.DataFrame) -> pd.DataFrame:
    """Add a chronological numeric key for quarter comparisons."""
    out = df.copy()
    keys = out["quarter"].map(quarter_sort_key)
    out["_quarter_order"] = [year * 10 + q for year, q in keys]
    return out


def load_inputs(
    forecast_errors_path: Path,
    estimates_path: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load and validate the existing master CSVs."""
    if not forecast_errors_path.exists():
        raise FileNotFoundError(
            f"Missing {forecast_errors_path}. Run the main pipeline first."
        )

    if not estimates_path.exists():
        raise FileNotFoundError(
            f"Missing {estimates_path}. Run the main pipeline first."
        )

    fe = pd.read_csv(forecast_errors_path)
    est = pd.read_csv(estimates_path)

    required_fe = {
        "analyst",
        "firm",
        "quarter",
        "fe",
    }
    required_est = {
        "firm",
        "quarter",
        "analyst",
        "estimate_value",
        "actual_eps",
        "price_10d_prior",
    }

    missing_fe = sorted(required_fe - set(fe.columns))
    missing_est = sorted(required_est - set(est.columns))

    if missing_fe:
        raise ValueError(
            f"{forecast_errors_path} is missing required columns: {missing_fe}"
        )

    if missing_est:
        raise ValueError(
            f"{estimates_path} is missing required columns: {missing_est}"
        )

    fe["fe"] = pd.to_numeric(fe["fe"], errors="coerce")
    est["estimate_value"] = pd.to_numeric(
        est["estimate_value"], errors="coerce"
    )
    est["actual_eps"] = pd.to_numeric(est["actual_eps"], errors="coerce")
    est["price_10d_prior"] = pd.to_numeric(
        est["price_10d_prior"], errors="coerce"
    )

    fe = fe.dropna(
        subset=["analyst", "firm", "quarter", "fe"]
    ).copy()

    est = est.dropna(
        subset=[
            "analyst",
            "firm",
            "quarter",
            "estimate_value",
            "actual_eps",
            "price_10d_prior",
        ]
    ).copy()

    est = est[est["price_10d_prior"] != 0].copy()

    fe = add_quarter_order(fe)
    est = add_quarter_order(est)

    return fe, est


def calculate_historical_weights(
    forecast_errors: pd.DataFrame,
    target_quarter_order: int,
    min_history_obs: int,
) -> pd.DataFrame:
    """
    Calculate each analyst's target-date weight using only prior quarters.

    Historical MAE is based on the existing normalized forecast error `fe`.
    Better historical accuracy receives more weight. More observations
    increase credibility through shrinkage toward a neutral prior.
    """
    prior = forecast_errors[
        forecast_errors["_quarter_order"] < target_quarter_order
    ].copy()

    if prior.empty:
        return pd.DataFrame(
            columns=[
                "analyst",
                "n_prior_obs",
                "historical_mae",
                "accuracy_component",
                "credibility_weight",
                "raw_weight",
                "normalized_weight",
            ]
        )

    stats = (
        prior.groupby("analyst", as_index=False)
        .agg(
            n_prior_obs=("fe", "count"),
            historical_mae=(
                "fe",
                lambda s: float(np.mean(np.abs(s))),
            ),
        )
    )

    stats = stats[stats["n_prior_obs"] >= min_history_obs].copy()

    if stats.empty:
        return stats.assign(
            accuracy_component=pd.Series(dtype=float),
            credibility_weight=pd.Series(dtype=float),
            raw_weight=pd.Series(dtype=float),
            normalized_weight=pd.Series(dtype=float),
        )

    stats["accuracy_component"] = 1.0 / stats["historical_mae"].clip(
        lower=MAE_FLOOR
    )

    stats["credibility_weight"] = (
        stats["n_prior_obs"]
        / (stats["n_prior_obs"] + CREDIBILITY_PRIOR_OBS)
    )

    stats["raw_weight"] = (
        stats["accuracy_component"]
        * stats["credibility_weight"]
    )

    total = float(stats["raw_weight"].sum())

    if total <= 0:
        stats["normalized_weight"] = 0.0
    else:
        stats["normalized_weight"] = stats["raw_weight"] / total

    return stats.sort_values(
        "normalized_weight",
        ascending=False,
    ).reset_index(drop=True)


def calculate_target(
    firm: str,
    quarter: str,
    target_quarter_order: int,
    estimates: pd.DataFrame,
    forecast_errors: pd.DataFrame,
    min_history_obs: int,
) -> tuple[dict, pd.DataFrame]:
    """Calculate Standard Consensus for every target; Smart when eligible."""
    current = estimates[
        (estimates["firm"] == firm)
        & (estimates["quarter"] == quarter)
        & (estimates["_quarter_order"] == target_quarter_order)
    ].copy()

    current = current.dropna(
        subset=["estimate_value", "actual_eps", "price_10d_prior"]
    )

    if current.empty:
        return {}, pd.DataFrame()

    current = current.sort_values(
        ["analyst", "estimate_value"]
    ).drop_duplicates(
        subset=["analyst"],
        keep="last",
    )

    standard_consensus = float(
        current["estimate_value"].median()
    )

    actual_eps = float(
        current["actual_eps"].iloc[0]
    )
    price_10d_prior = float(
        current["price_10d_prior"].iloc[0]
    )

    standard_fe = (
        standard_consensus - actual_eps
    ) / price_10d_prior

    weights = calculate_historical_weights(
        forecast_errors=forecast_errors,
        target_quarter_order=target_quarter_order,
        min_history_obs=min_history_obs,
    )

    current = current.merge(
        weights[
            [
                "analyst",
                "n_prior_obs",
                "historical_mae",
                "accuracy_component",
                "credibility_weight",
                "normalized_weight",
            ]
        ],
        on="analyst",
        how="left",
    )

    current["normalized_weight"] = current[
        "normalized_weight"
    ].fillna(0.0)

    weighted = current[
        current["normalized_weight"] > 0
    ].copy()

    if weighted.empty:
        smart_consensus = np.nan
        smart_fe = np.nan
        smart_abs_fe = np.nan
        smart_n = 0
        smart_coverage = 0.0
        winner = "Smart unavailable"
    else:
        weight_sum = float(
            weighted["normalized_weight"].sum()
        )

        if weight_sum <= 0:
            smart_consensus = np.nan
            smart_fe = np.nan
            smart_abs_fe = np.nan
            smart_n = 0
            smart_coverage = 0.0
            winner = "Smart unavailable"
        else:
            weighted["final_weight"] = (
                weighted["normalized_weight"]
                / weight_sum
            )

            smart_consensus = float(
                (
                    weighted["estimate_value"]
                    * weighted["final_weight"]
                ).sum()
            )

            smart_fe = (
                smart_consensus - actual_eps
            ) / price_10d_prior

            smart_abs_fe = abs(smart_fe)
            smart_n = int(
                weighted["analyst"].nunique()
            )
            smart_coverage = (
                smart_n
                / current["analyst"].nunique()
            )

            if abs(smart_fe) < abs(standard_fe):
                winner = "Smart"
            elif abs(smart_fe) > abs(standard_fe):
                winner = "Standard"
            else:
                winner = "Tie"

    result = {
        "firm": firm,
        "quarter": quarter,
        "standard_consensus": standard_consensus,
        "smart_consensus": smart_consensus,
        "actual_eps": actual_eps,
        "price_10d_prior": price_10d_prior,
        "standard_fe": standard_fe,
        "smart_fe": smart_fe,
        "standard_abs_fe": abs(standard_fe),
        "smart_abs_fe": smart_abs_fe,
        "n_current_estimates": int(
            current["analyst"].nunique()
        ),
        "n_smart_weighted_analysts": smart_n,
        "smart_weight_coverage": smart_coverage,
        "smart_available": bool(
            pd.notna(smart_consensus)
        ),
        "winner": winner,
    }

    detail = current.copy()
    detail["firm"] = firm
    detail["quarter"] = quarter
    detail["target_quarter_order"] = target_quarter_order

    return result, detail


def build_model(
    forecast_errors: pd.DataFrame,
    estimates: pd.DataFrame,
    min_history_obs: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build all target firm-quarter rows, even when Smart is unavailable."""
    targets = (
        estimates[
            ["firm", "quarter", "_quarter_order"]
        ]
        .drop_duplicates()
        .sort_values(
            ["_quarter_order", "firm"]
        )
        .reset_index(drop=True)
    )

    prediction_rows = []
    weight_rows = []

    for firm, quarter, target_quarter_order in targets.itertuples(
        index=False,
        name=None,
    ):
        result, detail = calculate_target(
            firm=firm,
            quarter=quarter,
            target_quarter_order=target_quarter_order,
            estimates=estimates,
            forecast_errors=forecast_errors,
            min_history_obs=min_history_obs,
        )

        if not result:
            continue

        prediction_rows.append(result)

        if not detail.empty:
            keep = [
                c
                for c in [
                    "firm",
                    "quarter",
                    "target_quarter_order",
                    "analyst",
                    "broker",
                    "broker_code",
                    "estimate_value",
                    "n_prior_obs",
                    "historical_mae",
                    "accuracy_component",
                    "credibility_weight",
                    "normalized_weight",
                    "final_weight",
                ]
                if c in detail.columns
            ]

            detail = detail[keep].copy()
            detail = detail[
                detail.get("normalized_weight", 0).fillna(0) > 0
            ]

            if not detail.empty:
                weight_rows.append(detail)

    predictions = pd.DataFrame(
        prediction_rows
    )

    weights = (
        pd.concat(
            weight_rows,
            ignore_index=True,
        )
        if weight_rows
        else pd.DataFrame()
    )

    return predictions, weights


def build_summary(predictions: pd.DataFrame) -> pd.DataFrame:
    """Build a coverage-aware Standard vs Smart summary."""
    if predictions.empty:
        return pd.DataFrame()

    total = len(predictions)
    smart_available = int(
        predictions["smart_available"].sum()
    )
    smart_unavailable = total - smart_available

    evaluated_smart = predictions[
        predictions["smart_available"]
    ].copy()

    if not evaluated_smart.empty:
        smart_wins = int(
            (evaluated_smart["winner"] == "Smart").sum()
        )
        standard_wins = int(
            (evaluated_smart["winner"] == "Standard").sum()
        )
        ties = int(
            (evaluated_smart["winner"] == "Tie").sum()
        )

        standard_mae = float(
            evaluated_smart["standard_abs_fe"].mean()
        )
        smart_mae = float(
            evaluated_smart["smart_abs_fe"].mean()
        )

        standard_median = float(
            evaluated_smart["standard_abs_fe"].median()
        )
        smart_median = float(
            evaluated_smart["smart_abs_fe"].median()
        )

        smart_win_rate = smart_wins / len(evaluated_smart)
        standard_win_rate = (
            standard_wins / len(evaluated_smart)
        )
        improvement = standard_mae - smart_mae
    else:
        smart_wins = 0
        standard_wins = 0
        ties = 0
        standard_mae = np.nan
        smart_mae = np.nan
        standard_median = np.nan
        smart_median = np.nan
        smart_win_rate = np.nan
        standard_win_rate = np.nan
        improvement = np.nan

    return pd.DataFrame(
        [
            {
                "metric": "All target firm quarters",
                "standard": total,
                "smart": total,
            },
            {
                "metric": "Smart Consensus available",
                "standard": smart_available,
                "smart": smart_available,
            },
            {
                "metric": "Smart Consensus unavailable",
                "standard": smart_unavailable,
                "smart": smart_unavailable,
            },
            {
                "metric": "Smart coverage",
                "standard": np.nan,
                "smart": smart_available / total,
            },
            {
                "metric": "Evaluated Smart vs Standard quarters",
                "standard": len(evaluated_smart),
                "smart": len(evaluated_smart),
            },
            {
                "metric": "Mean absolute forecast error",
                "standard": standard_mae,
                "smart": smart_mae,
            },
            {
                "metric": "Median absolute forecast error",
                "standard": standard_median,
                "smart": smart_median,
            },
            {
                "metric": "Wins",
                "standard": standard_wins,
                "smart": smart_wins,
            },
            {
                "metric": "Win rate",
                "standard": standard_win_rate,
                "smart": smart_win_rate,
            },
            {
                "metric": "Ties",
                "standard": ties,
                "smart": ties,
            },
            {
                "metric": "Smart improvement vs Standard",
                "standard": np.nan,
                "smart": improvement,
            },
        ]
    )


def analyst_aggregate_scores(
    forecast_errors: pd.DataFrame,
    weights: pd.DataFrame,
) -> pd.DataFrame:
    """
    One row per analyst, combining EVERY observation she has across every
    ticker in your pull -- not per-firm, not per-run. An analyst who covers
    CME-US and TSM-US and GS-US gets all of that pooled into one score here.

    This is deliberately a separate, simpler view from the out-of-sample
    Smart Consensus weighting above: it's a plain in-sample summary (mean
    absolute forecast error across her full history, most-recent-first
    doesn't matter here) meant for "who's actually good, full stop" rather
    than "whose estimate should I trust for THIS specific upcoming quarter".
    `historical_mae` in the out-of-sample weights table is the more rigorous
    number if you need something forward-looking; this one is easier to read
    at a glance across your whole analyst universe.

    Columns:
      analyst, n_observations, n_firms_covered, firms_covered,
      avg_abs_fe (lower is better), n_times_smart_weighted,
      avg_weight_when_smart_weighted (higher means Smart Consensus leaned
      on her more, i.e. she had a strong enough track record to earn real
      influence over the blended estimate).
    """
    if forecast_errors.empty:
        return pd.DataFrame(
            columns=[
                "analyst", "n_observations", "n_firms_covered", "firms_covered",
                "avg_abs_fe", "n_times_smart_weighted", "avg_weight_when_smart_weighted",
            ]
        )

    agg = (
        forecast_errors.groupby("analyst", as_index=False)
        .agg(
            n_observations=("fe", "count"),
            n_firms_covered=("firm", "nunique"),
            firms_covered=("firm", lambda s: ", ".join(sorted(set(map(str, s))))),
            avg_abs_fe=("fe", lambda s: float(np.mean(np.abs(s)))),
        )
    )

    if not weights.empty and {"analyst", "final_weight"}.issubset(weights.columns):
        w = weights.copy()
        w["final_weight"] = pd.to_numeric(w["final_weight"], errors="coerce")
        w_agg = (
            w.groupby("analyst", as_index=False)
            .agg(
                n_times_smart_weighted=("final_weight", "count"),
                avg_weight_when_smart_weighted=("final_weight", "mean"),
            )
        )
        agg = agg.merge(w_agg, on="analyst", how="left")
    else:
        agg["n_times_smart_weighted"] = 0
        agg["avg_weight_when_smart_weighted"] = np.nan

    agg["n_times_smart_weighted"] = agg["n_times_smart_weighted"].fillna(0).astype(int)

    return agg.sort_values(
        ["avg_abs_fe", "n_observations"], ascending=[True, False]
    ).reset_index(drop=True)


def sector_leaderboard(forecast_errors: pd.DataFrame) -> pd.DataFrame:
    """
    Rank analysts WITHIN their industry (Fama-French 48 grouping, resolved
    from each firm's SIC code) instead of across your whole ticker universe.
    Answers "who's the best semiconductor-covering analyst", not just
    "who's the best analyst overall".

    Honesty check baked into the output: `n_tickers_in_sector` tells you, at
    a glance, whether a given sector ranking actually means anything yet. A
    sector with only 1 ticker in your current pull can't really compare
    analysts against SECTOR peers -- it's just that one ticker's leaderboard
    wearing a sector label. As you pull more tickers per industry, these
    rankings become genuinely comparative.

    Requires forecast_errors to have a `sic_code` column (present in every
    live_<TICKER>_raw_forecast_errors.csv / master_raw_forecast_errors.csv
    this project produces). Rows with no resolvable industry are dropped
    with a printed warning rather than silently mis-bucketed.
    """
    if forecast_errors.empty or "sic_code" not in forecast_errors.columns:
        return pd.DataFrame(
            columns=[
                "industry", "analyst", "n_observations", "avg_abs_fe",
                "n_firms_covered_in_sector", "n_tickers_in_sector",
            ]
        )

    from src.ff48_industries import sic_to_industry

    df = forecast_errors.dropna(subset=["sic_code"]).copy()

    def _industry_name(sic):
        try:
            result = sic_to_industry(int(sic))
        except Exception:
            return None
        return result.industry_name if result else None

    df["industry"] = df["sic_code"].map(_industry_name)
    unresolved = df["industry"].isna().sum()
    if unresolved:
        print(f"sector_leaderboard(): {unresolved} row(s) had a SIC code with no FF48 "
              f"industry match -- dropped from the sector view (they still count "
              f"normally everywhere else).")
    df = df.dropna(subset=["industry"])
    if df.empty:
        return pd.DataFrame(
            columns=[
                "industry", "analyst", "n_observations", "avg_abs_fe",
                "n_firms_covered_in_sector", "n_tickers_in_sector",
            ]
        )

    sector_ticker_counts = df.groupby("industry")["firm"].nunique().rename("n_tickers_in_sector")

    agg = (
        df.groupby(["industry", "analyst"], as_index=False)
        .agg(
            n_observations=("fe", "count"),
            avg_abs_fe=("fe", lambda s: float(np.mean(np.abs(s)))),
            n_firms_covered_in_sector=("firm", "nunique"),
        )
    )
    agg = agg.merge(sector_ticker_counts, on="industry", how="left")

    return agg.sort_values(
        ["industry", "avg_abs_fe"], ascending=[True, True]
    ).reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build the out-of-sample Smart Consensus model "
            "from existing master CSVs. No FactSet/API calls."
        )
    )

    parser.add_argument(
        "--forecast-errors",
        default=str(
            DEFAULT_FORECAST_ERRORS
        ),
    )

    parser.add_argument(
        "--estimates",
        default=str(
            DEFAULT_ESTIMATES
        ),
    )

    parser.add_argument(
        "--min-history",
        type=int,
        default=MIN_HISTORY_OBS,
        help=(
            "Minimum prior observations required "
            "before an analyst can receive a Smart "
            "Consensus weight."
        ),
    )

    parser.add_argument(
        "--output-dir",
        default=str(
            OUTPUT_DIR
        ),
    )

    args = parser.parse_args()

    min_history_obs = int(
        args.min_history
    )

    output_dir = Path(
        args.output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    fe, estimates = load_inputs(
        forecast_errors_path=Path(
            args.forecast_errors
        ),
        estimates_path=Path(
            args.estimates
        ),
    )

    predictions, weights = build_model(
        forecast_errors=fe,
        estimates=estimates,
        min_history_obs=min_history_obs,
    )

    summary = build_summary(
        predictions
    )

    predictions_path = (
        output_dir
        / "smart_consensus_predictions.csv"
    )

    weights_path = (
        output_dir
        / "smart_consensus_analyst_weights.csv"
    )

    summary_path = (
        output_dir
        / "smart_consensus_summary.csv"
    )

    predictions.to_csv(
        predictions_path,
        index=False,
    )

    weights.to_csv(
        weights_path,
        index=False,
    )

    summary.to_csv(
        summary_path,
        index=False,
    )

    analyst_aggregate = analyst_aggregate_scores(fe, weights)
    analyst_aggregate_path = output_dir / "smart_consensus_analyst_aggregate.csv"
    analyst_aggregate.to_csv(analyst_aggregate_path, index=False)

    sector_lb = sector_leaderboard(fe)
    sector_leaderboard_path = output_dir / "smart_consensus_sector_leaderboard.csv"
    sector_lb.to_csv(sector_leaderboard_path, index=False)

    print("=== SMART CONSENSUS ===")
    print("FactSet/API calls: 0")
    print(
        f"Forecast-error rows: {len(fe):,}"
    )
    print(
        f"Estimate rows: {len(estimates):,}"
    )
    print(
        f"Evaluated firm-quarters: {len(predictions):,}"
    )

    if not summary.empty:
        print("\nPerformance:")
        print(
            summary.to_string(
                index=False
            )
        )

    if not analyst_aggregate.empty:
        print(f"\nAnalyst aggregate: {len(analyst_aggregate)} analyst(s), pooled across every "
              f"ticker they cover -- see {analyst_aggregate_path}")

    if not sector_lb.empty:
        n_sectors = sector_lb["industry"].nunique()
        print(f"Sector leaderboard: {n_sectors} industry sector(s) -- see {sector_leaderboard_path}")
    else:
        print("Sector leaderboard: no rows produced (no resolvable SIC codes in this data).")

    print("\nWrote:")
    print(predictions_path)
    print(weights_path)
    print(summary_path)
    print(analyst_aggregate_path)
    print(sector_leaderboard_path)


if __name__ == "__main__":
    main()
