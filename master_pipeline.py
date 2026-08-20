"""
master_pipeline.py
===================
ONE file that implements the full methodology of:

  Chhaochharia, V., Kumar, A., Rantala, V., Zhang, L. (2022)
  "Artificially Intelligent Analyst Sentiment and Aggregate Market Behavior"

...plus one extension of our own: a per-ANALYST "reliability" score (the
paper itself only scores sentiment at the firm/industry/market level --
see the note in section 6 below for why, and what we do instead).

HOW TO READ THIS FILE
----------------------
It's organized in the same order as the paper's Section 2 methodology,
each section header names the paper's equation it implements:

  1. Forecast Error                       (paper Eq. 1,  p.11)
  2. Per-analyst NN + linear models        (paper Eq. 2/3, p.14-15)
  3. Winsorization + consensus aggregation (paper p.16)
  4. Industry- and market-level aggregation(paper Eq. 4/5, p.17-18)
  5. Industry-level analyst sentiment +
     Long-Short portfolio construction     (paper Eq. 6,   p.31)
  6. ANALYST-LEVEL RELIABILITY SCORE       (our extension -- see docstring)
  7. FACTOR-MODEL RISK ADJUSTMENT          (paper Eq. 6, p.31: "four-factor
                                             alpha 0.62%, t=2.48" -- Carhart
                                             4-factor / FFC4; alternative FF5+MOM)
  8. FactSet data pull (Formula API)       (this account's only entitled
                                             product -- see README.md)
  9. Orchestration / CLI (--mock / --live)

RUNNING IT
----------
    python master_pipeline.py --mock            # synthetic data, 0 API calls
    python master_pipeline.py --live --ticker AAPL-US    # real FactSet calls,
                                                          # budget-guarded

Mock mode exists so every formula below can be verified end-to-end (against
hand-computable numbers) without spending a single API point. Only run
--live once you're ready to spend real calls -- see CALL BUDGET below.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.neural_network import MLPRegressor

sys.path.insert(0, os.path.dirname(__file__))
from src.ff48_industries import sic_to_industry  # noqa: E402
from src.factors import load_factors, run_factor_backtest as run_factor_bt, FactorBacktestResult, compute_analyst_factor_alpha  # noqa: E402


# =============================================================================
# 1. FORECAST ERROR  --  paper Eq. 1 (p.11)
#
#   FE_{i,j,d} = (x_hat_{i,j,d} - x_{j,d}) / P_{j,d-10}
#
#   x_hat = analyst i's EPS forecast for firm j at date d
#   x     = firm j's actual reported EPS
#   P_{j,d-10} = firm j's share price 10 TRADING days before the earnings
#                announcement
#
#   Positive FE => analyst forecast was too HIGH => optimistic.
# =============================================================================

def forecast_error(eps_forecast: float, eps_actual: float, price_10d_prior: float) -> float:
    if price_10d_prior in (0, None) or price_10d_prior != price_10d_prior:  # NaN-safe
        return float("nan")
    return (eps_forecast - eps_actual) / price_10d_prior


# =============================================================================
# 2. PER-ANALYST MODELS  --  paper Eq. 2 (NN) and Eq. 3 (linear), p.14-15
#
#   FE_{i,j,q} = f(w1*FE_{i,j,q-1} + w2*FE_{i,j,q-2} + w3*FE_{i,j,q-3}
#                  + w4*FE_{i,j,q-4} + alpha)
#
#   - One model PER ANALYST, trained on THAT analyst's own forecast errors
#     across every firm she covers (not per firm) -- this is what lets the
#     model learn her personal, systematic bias.
#   - Features: her own 4 lagged quarterly forecast errors for firm j.
#   - Re-trained every year on an expanding window (all data up to year t-1).
#   - Paper requires >=10 historical forecasts before an analyst enters
#     training; uses a 2-layer feed-forward NN, 1 hidden layer, 15 neurons,
#     Levenberg-Marquardt optimizer.
#
#   DEVIATION FROM THE PAPER (documented, not hidden): scikit-learn's
#   MLPRegressor does not offer Levenberg-Marquardt (that's a MATLAB
#   nntool-specific second-order optimizer). We use solver="lbfgs", which is
#   the closest practical equivalent available in Python for small/sparse
#   per-analyst training sets like this (also second-order, quasi-Newton).
#   If you have MATLAB or want an exact LM re-implementation, swap the model
#   class in train_analyst_models() below -- everything downstream is
#   agnostic to which model produced the prediction.
# =============================================================================

MIN_TRAINING_OBS = 10  # paper's minimum forecast count before training (p.14)
N_LAGS = 4


def _build_lag_features(analyst_fe: pd.DataFrame) -> pd.DataFrame:
    """
    analyst_fe: columns [firm, quarter (sortable, e.g. '2019Q1'), fe]
    for ONE analyst. Returns a frame with columns
    [firm, quarter, fe, lag1, lag2, lag3, lag4] built by shifting within
    each firm's own time series (matches the paper: lags are same-firm).
    """
    out = []
    for firm, g in analyst_fe.sort_values("quarter").groupby("firm"):
        g = g.copy()
        for lag in range(1, N_LAGS + 1):
            g[f"lag{lag}"] = g["fe"].shift(lag)
        out.append(g)
    return pd.concat(out, ignore_index=True) if out else analyst_fe.assign(
        **{f"lag{i}": [] for i in range(1, N_LAGS + 1)}
    )


@dataclass
class AnalystModelBundle:
    analyst_id: str
    nn_model: Optional[MLPRegressor] = None
    linear_model: Optional[LinearRegression] = None
    n_train_obs: int = 0
    trained: bool = False


def train_analyst_models(analyst_fe: pd.DataFrame, analyst_id: str, random_state: int = 0) -> AnalystModelBundle:
    """
    Trains BOTH the NN (Eq.2) and linear (Eq.3) model for one analyst on her
    full available history (an "expanding window" call site decides how much
    history to pass in -- see run_expanding_window() below).
    """
    lagged = _build_lag_features(analyst_fe).dropna(subset=[f"lag{i}" for i in range(1, N_LAGS + 1)])
    bundle = AnalystModelBundle(analyst_id=analyst_id, n_train_obs=len(lagged))

    if len(lagged) < MIN_TRAINING_OBS:
        return bundle  # not enough history yet -- paper's rule, p.14

    X = lagged[[f"lag{i}" for i in range(1, N_LAGS + 1)]].values
    y = lagged["fe"].values

    linear = LinearRegression().fit(X, y)

    nn = MLPRegressor(
        hidden_layer_sizes=(15,),   # paper: 1 hidden layer, 15 neurons (p.14)
        solver="lbfgs",             # closest practical stand-in for Levenberg-Marquardt
        max_iter=2000,
        random_state=random_state,
    ).fit(X, y)

    bundle.linear_model = linear
    bundle.nn_model = nn
    bundle.trained = True
    return bundle


def predict_forecast_error(bundle: AnalystModelBundle, lag_values: list) -> tuple:
    """Returns (nn_predicted_fe, linear_predicted_fe) for one (analyst, firm, quarter)."""
    if not bundle.trained:
        return float("nan"), float("nan")
    X = np.array(lag_values).reshape(1, -1)
    return float(bundle.nn_model.predict(X)[0]), float(bundle.linear_model.predict(X)[0])


def run_expanding_window(all_analyst_fe: pd.DataFrame) -> pd.DataFrame:
    """
    Implements the paper's expanding-window, re-estimate-every-year procedure
    (p.14-15) for EVERY analyst in the input frame.

    all_analyst_fe columns: [analyst, firm, quarter, year, fe]
    (quarter is a sortable label like '2019Q1'; year is int)

    Returns the input frame with two new columns:
      nn_predicted_fe, linear_predicted_fe
    populated OUT-OF-SAMPLE: for each analyst-year t, the model is trained
    only on her data strictly before year t, then used to predict all her
    quarters within year t.
    """
    results = []
    for analyst, adf in all_analyst_fe.groupby("analyst"):
        years = sorted(adf["year"].unique())
        for t in years:
            train_slice = adf[adf["year"] < t]
            predict_slice = adf[adf["year"] == t]
            if train_slice.empty:
                # nothing to train on yet -- leave predictions as NaN for year t
                out = predict_slice.copy()
                out["nn_predicted_fe"] = np.nan
                out["linear_predicted_fe"] = np.nan
                results.append(out)
                continue

            bundle = train_analyst_models(train_slice[["firm", "quarter", "fe"]], analyst)

            # Need lag features for the prediction rows too, built from the
            # continuous per-firm history (train + predict slices combined).
            combined = pd.concat([train_slice, predict_slice]).sort_values("quarter")
            lagged = _build_lag_features(combined[["firm", "quarter", "fe"]])
            lagged = lagged.merge(
                predict_slice[["firm", "quarter"]], on=["firm", "quarter"], how="inner"
            )

            nn_preds, lin_preds = [], []
            for _, row in lagged.iterrows():
                lag_vals = [row[f"lag{i}"] for i in range(1, N_LAGS + 1)]
                if any(pd.isna(lag_vals)):
                    nn_preds.append(np.nan)
                    lin_preds.append(np.nan)
                else:
                    nn_p, lin_p = predict_forecast_error(bundle, lag_vals)
                    nn_preds.append(nn_p)
                    lin_preds.append(lin_p)

            out = predict_slice.copy().sort_values("quarter")
            out["nn_predicted_fe"] = nn_preds
            out["linear_predicted_fe"] = lin_preds
            results.append(out)

    return pd.concat(results, ignore_index=True) if results else all_analyst_fe.assign(
        nn_predicted_fe=np.nan, linear_predicted_fe=np.nan
    )


# =============================================================================
# 3. WINSORIZATION + CONSENSUS AGGREGATION  --  paper p.16
#
#   - Actual Forecast Error winsorized at 1% extremes.
#   - NN- and linear-predicted FE winsorized using the SAME thresholds
#     (i.e. the cutoffs are computed from actual FE, then applied to the
#     predicted series too).
#   - Consensus Forecast Error (firm-quarter) = median across analysts
#     covering that earnings announcement (paper uses median because I/B/E/S
#     consensus is conventionally a median).
#   - Predicted Consensus Forecast Error = median of NN-predicted FE across
#     the same analysts (paper p.17). Same for linear.
# =============================================================================

def winsorize(series: pd.Series, pct: float = 0.01) -> pd.Series:
    lo, hi = series.quantile(pct), series.quantile(1 - pct)
    return series.clip(lower=lo, upper=hi)


def winsorize_with_thresholds(series: pd.Series, lo: float, hi: float) -> pd.Series:
    return series.clip(lower=lo, upper=hi)


def to_consensus(df: pd.DataFrame) -> pd.DataFrame:
    """
    df: one row per (analyst, firm, quarter) with fe, nn_predicted_fe,
    linear_predicted_fe, market_cap (winsorized already).
    Returns one row per (firm, quarter) with the median actual/predicted FE
    -- the "Consensus Forecast Error" / "Predicted Consensus Forecast Error"
    of the paper (p.16-17).
    """
    return (
        df.groupby(["firm", "quarter"])
        .agg(
            consensus_fe=("fe", "median"),
            consensus_nn_predicted_fe=("nn_predicted_fe", "median"),
            consensus_linear_predicted_fe=("linear_predicted_fe", "median"),
            market_cap=("market_cap", "first"),  # firm-level, same for every analyst row
            sic_code=("sic_code", "first"),
        )
        .reset_index()
    )


# =============================================================================
# 4. INDUSTRY- AND MARKET-LEVEL AGGREGATION  --  paper Eq. 4 (p.17) & Eq. 5 (p.18)
#
#   Industry_{s,q}  = sum_j( w_{j,q} * c_{j,q} ) / sum_j( w_{j,q} )   for j in industry s
#   Market_q        = sum_j( w_{j,q} * c_{j,q} ) / sum_j( w_{j,q} )   for j in ALL firms
#
#   w_{j,q} = firm j's market cap 10 trading days before its earnings date
#   c_{j,q} = the (actual, NN-predicted, or linear-predicted) Consensus
#             Forecast Error of firm j in quarter q
#
#   Industries are the 48 Fama-French industries (src/ff48_industries.py).
# =============================================================================

def add_ff48_industry(consensus_df: pd.DataFrame) -> pd.DataFrame:
    consensus_df = consensus_df.copy()
    consensus_df["industry"] = consensus_df["sic_code"].apply(
        lambda s: (sic_to_industry(s).industry_name if sic_to_industry(s) else "UNMAPPED")
    )
    return consensus_df


def _mktcap_weighted_avg(df: pd.DataFrame, value_col: str) -> float:
    w = df["market_cap"]
    v = df[value_col]
    mask = w.notna() & v.notna()
    if mask.sum() == 0 or w[mask].sum() == 0:
        return float("nan")
    return float((w[mask] * v[mask]).sum() / w[mask].sum())


def industry_level(consensus_df: pd.DataFrame) -> pd.DataFrame:
    """Eq. 4 -- one row per (industry, quarter)."""
    rows = []
    for (industry, quarter), g in consensus_df.groupby(["industry", "quarter"]):
        rows.append(
            {
                "industry": industry,
                "quarter": quarter,
                "industry_fe": _mktcap_weighted_avg(g, "consensus_fe"),
                "industry_nn_predicted_fe": _mktcap_weighted_avg(g, "consensus_nn_predicted_fe"),
                "industry_linear_predicted_fe": _mktcap_weighted_avg(g, "consensus_linear_predicted_fe"),
                "n_firms": len(g),
            }
        )
    out = pd.DataFrame(rows)
    if not out.empty:
        out["industry_sentiment"] = out["industry_fe"] - out["industry_nn_predicted_fe"]
    return out


def market_level(consensus_df: pd.DataFrame) -> pd.DataFrame:
    """Eq. 5 -- one row per quarter, across ALL firms."""
    rows = []
    for quarter, g in consensus_df.groupby("quarter"):
        rows.append(
            {
                "quarter": quarter,
                "market_fe": _mktcap_weighted_avg(g, "consensus_fe"),
                "market_nn_predicted_fe": _mktcap_weighted_avg(g, "consensus_nn_predicted_fe"),
                "market_linear_predicted_fe": _mktcap_weighted_avg(g, "consensus_linear_predicted_fe"),
                "n_firms": len(g),
            }
        )
    out = pd.DataFrame(rows)
    if not out.empty:
        out["market_sentiment"] = out["market_fe"] - out["market_nn_predicted_fe"]
    return out


# =============================================================================
# 5. INDUSTRY-LEVEL ANALYST SENTIMENT + LONG-SHORT PORTFOLIO -- paper Eq. 6 (p.31)
#    and the Long-Short strategy description (p.31, Section 4.5)
#
#   Monthly (paper) sort of the 48 industries by Industry-Level Analyst
#   Sentiment; Long = 5 lowest-sentiment industries, Short = 5 highest,
#   value-weighted, rebalanced monthly. Included here for completeness /
#   future replication -- NOT the main ask this session, and not something
#   we can validate meaningfully on a single ticker.
# =============================================================================

def long_short_industry_sort(industry_df: pd.DataFrame, quarter: str, n_per_leg: int = 5) -> dict:
    """
    Given industry_level() output and one quarter/month label, returns the
    Long (lowest sentiment) and Short (highest sentiment) industry lists per
    paper Eq. 6 / p.31. Needs >= 2*n_per_leg industries with valid sentiment
    in that period to be meaningful.
    """
    period = industry_df[industry_df["quarter"] == quarter].dropna(subset=["industry_sentiment"])
    period = period.sort_values("industry_sentiment")
    return {
        "long": period.head(n_per_leg)["industry"].tolist(),
        "short": period.tail(n_per_leg)["industry"].tolist(),
        "n_available": len(period),
    }


# =============================================================================
# 6. ANALYST-LEVEL RELIABILITY SCORE  --  OUR EXTENSION
#
#   IMPORTANT, READ THIS: the paper (Chhaochharia et al., 2022) does NOT
#   publish a per-analyst "who should I trust" score. Its NN models ARE
#   trained per-analyst (Eq. 2), but the paper only ever reports results
#   aggregated to the firm/industry/market level -- its entire contribution
#   is the AGGREGATE "analyst sentiment" measure and the industry Long-Short
#   strategy built from it (Sections 3-4).
#
#   What follows is Patrick's requested extension, built using the SAME
#   underlying decomposition the paper validates (predictable bias vs.
#   unpredictable "sentiment" residual), just stopped one level earlier --
#   at the analyst instead of the industry/market:
#
#     accuracy_score      = -mean(|actual FE|)              (higher = better)
#     predictability_score = out-of-sample R^2 of her own NN model
#                            (higher = her bias is systematic/learnable,
#                             not random noise -- the paper's whole point
#                             is that systematic bias is DIFFERENT from
#                             pure noise, and only noise should count
#                             against reliability)
#     consistency_score    = -std(actual FE - nn_predicted_fe)
#                            (her own "sentiment" residual volatility --
#                             lower = more internally consistent once you
#                             net out her systematic bias)
#
#   Composite = equal-weighted average of the three z-scores. This is a
#   REASONABLE, DEFENSIBLE construction consistent with the paper's logic,
#   but it is explicitly OUR addition, not a result from the paper itself.
#
#   BROKER ATTRIBUTION (Patrick's follow-up request): an analyst's employer
#   is a per-QUARTER fact, not a fixed label -- she can move firms mid-
#   history, and FactSet's FE_BROKER_ESTIMATE snapshot credits each estimate
#   to whichever broker she was AT when she made it. _broker_fields() below
#   surfaces four things per analyst: current_broker / current_broker_code
#   (her broker as of the MOST RECENT quarter in the data passed in -- "the
#   brokerage firm related to the analyst at that time", per Patrick's
#   wording) and n_brokers (how many distinct brokers she was credited to
#   across the whole window -- >1 means she changed employers during the
#   period this run covers).
#
#   NAME vs. CODE: BKR_NAME is free text and can drift in spelling/
#   punctuation across snapshots; BKR_CODE (confirmed working -- Patrick's
#   candidate formula, verified against real AAPL-US data) is a stable
#   numeric broker identifier that doesn't have that problem, and stays
#   populated even on rows where the analyst's NAME shows as 'Restricted'.
#   So n_brokers / de-duplication is computed from CODE when it's present
#   (falls back to name if a run doesn't have codes yet), while
#   current_broker (the human-readable name) stays around for display.
# =============================================================================

def _broker_fields(g: pd.DataFrame) -> dict:
    """
    g: all rows for ONE analyst (any frame with optional 'broker' /
    'broker_code' columns, and ideally a 'quarter' column to sort by so
    "most recent" is correct). Returns {'current_broker', 'current_broker_code',
    'n_brokers'}. Safe to call on data with none of these columns at all
    (e.g. --mock data before you add them) -- returns None/0 rather than
    raising.
    """
    has_name = "broker" in g.columns and not g["broker"].dropna().empty
    has_code = "broker_code" in g.columns and not g["broker_code"].dropna().empty
    if not has_name and not has_code:
        return {"current_broker": None, "current_broker_code": None, "n_brokers": 0}

    dedup_col = "broker_code" if has_code else "broker"
    g2 = g.dropna(subset=[dedup_col])
    g2 = g2.sort_values("quarter") if "quarter" in g2.columns else g2
    return {
        "current_broker": g2.iloc[-1]["broker"] if has_name else None,
        "current_broker_code": g2.iloc[-1]["broker_code"] if has_code else None,
        "n_brokers": int(g2[dedup_col].nunique()),
    }


def filter_by_broker(
    scores_df: pd.DataFrame,
    broker,
    by: str = "name",
    broker_col: Optional[str] = None,
) -> pd.DataFrame:
    """
    Filters an analyst-level scores/leaderboard frame (output of
    analyst_reliability_scores() or simple_accuracy_leaderboard(), both of
    which carry current_broker / current_broker_code via _broker_fields())
    down to analysts at one or more brokerages.

    broker: a single value OR a list of values -- e.g. "morgan", or
      ["morgan", "jpmorgan"], or (with by="code") [2295, 1080].
    by: "name" (default) -- case-insensitive SUBSTRING match on
      current_broker, e.g. "morgan" matches "Morgan Stanley". Convenient,
      but free-text names can drift in spelling/punctuation across
      snapshots, which can cause misses.
      "code" -- EXACT match on current_broker_code, FactSet's stable
      numeric broker identifier (see BROKER ATTRIBUTION note above). More
      reliable, especially when selecting several brokers at once, but you
      need to already know the code(s) -- check the current_broker_code
      column in your data first (or cross-reference against current_broker
      for the same rows).

    Returns an empty (not an error) frame if nothing matches, and handles
    the degenerate case where scores_df is EMPTY WITH NO COLUMNS AT ALL
    (analyst_reliability_scores() with zero trainable analysts) by
    returning it as-is rather than raising on the missing column.
    """
    if scores_df.empty:
        return scores_df

    col = broker_col or ("current_broker_code" if by == "code" else "current_broker")
    if col not in scores_df.columns:
        raise KeyError(
            f"'{col}' column not found -- filter_by_broker() only works on output from "
            f"analyst_reliability_scores() or simple_accuracy_leaderboard()."
        )

    values = broker if isinstance(broker, (list, tuple, set)) else [broker]

    if by == "code":
        mask = scores_df[col].isin(values)
    else:
        # re.escape() matters here -- brokerage names routinely contain regex-special
        # characters ("Cowen & Co.", "Wells Fargo Securities, L.L.C.") that would
        # otherwise be interpreted as regex syntax instead of literal characters.
        pattern = "|".join(re.escape(str(v)) for v in values)  # OR the substrings together
        mask = scores_df[col].fillna("").str.contains(pattern, case=False, na=False, regex=True)

    return scores_df[mask].reset_index(drop=True)


def analyst_reliability_scores(all_analyst_fe: pd.DataFrame) -> pd.DataFrame:
    """
    all_analyst_fe: output of run_expanding_window() -- needs columns
    [analyst, firm, quarter, year, fe, nn_predicted_fe], plus an OPTIONAL
    'broker' column (present when it came from fetch_live_ticker_data()) --
    see the BROKER ATTRIBUTION note above.
    Returns one row per analyst with the component scores, the RAW
    composite (reliability_composite_raw), a credibility_weight, and the
    final CREDIBILITY-WEIGHTED composite (reliability_composite -- this is
    what the frame is sorted/ranked by) -- plus current_broker / n_brokers.

    CREDIBILITY WEIGHTING (Patrick's request): an analyst with very few
    observations can land a great raw score by luck, and shouldn't outrank
    someone with a long, solid track record just because she got unlucky on
    a couple of calls. reliability_composite_raw is multiplied by
    n_predictions / (n_predictions + config.CREDIBILITY_PRIOR_OBS) -- this
    shrinks thin-evidence scores toward 0 (neutral) without touching
    well-observed analysts much. See config.CREDIBILITY_PRIOR_OBS's comment
    for why it's set to 10.

    FRESHNESS (Patrick's request): FactSet's per-estimate MODDATEN tells us
    when an analyst last actually TOUCHED her EPS number, separately from
    when we snapshotted it (always ANALYST_SNAPSHOT_DAYS_BEFORE_EARNINGS
    before the print). fetch_live_ticker_data() turns that into
    staleness_days = (snapshot_date - revision_date), so an analyst who
    updates her forecast right before the print has staleness_days near 0,
    and one who hasn't touched it in weeks has a large value. That becomes
    freshness_score = -avg(staleness_days) (higher = fresher = better, same
    "higher is better" sign convention as accuracy/consistency), z-scored and
    folded into the composite exactly like the other three components --
    an analyst who never revisits her number close to the event isn't
    giving you a live read, even if her stale number happens to look decent
    historically, so this docks her score. Only present when staleness_days
    data is available (--live pulls always have it now; older pulls or
    --mock runs that predate this still work fine without it -- the
    composite just averages over whichever components exist).
    """
    from src.config import CREDIBILITY_PRIOR_OBS
    has_staleness = "staleness_days" in all_analyst_fe.columns
    rows = []
    for analyst, g in all_analyst_fe.dropna(subset=["nn_predicted_fe"]).groupby("analyst"):
        actual = g["fe"].values
        pred = g["nn_predicted_fe"].values
        resid = actual - pred

        accuracy = -np.mean(np.abs(actual))

        ss_res = np.sum((actual - pred) ** 2)
        ss_tot = np.sum((actual - np.mean(actual)) ** 2)
        predictability = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan

        consistency = -np.std(resid)

        row = {
            "analyst": analyst,
            "n_predictions": len(g),
            "accuracy_score": accuracy,
            "predictability_r2": predictability,
            "consistency_score": consistency,
        }
        if has_staleness:
            staleness_vals = g["staleness_days"].dropna()
            row["avg_staleness_days"] = float(staleness_vals.mean()) if len(staleness_vals) else np.nan
        row.update(_broker_fields(g))
        rows.append(row)

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    if has_staleness and "avg_staleness_days" in out.columns:
        out["freshness_score"] = -out["avg_staleness_days"]

    z_source_cols = ["accuracy_score", "predictability_r2", "consistency_score"]
    if "freshness_score" in out.columns:
        z_source_cols.append("freshness_score")
    for col in z_source_cols:
        mu, sigma = out[col].mean(), out[col].std()
        out[f"{col}_z"] = (out[col] - mu) / sigma if sigma and sigma > 0 else 0.0

    z_cols = [f"{c}_z" for c in z_source_cols]
    out["reliability_composite_raw"] = out[z_cols].mean(axis=1)  # skips NaN per row automatically

    out["credibility_weight"] = out["n_predictions"] / (out["n_predictions"] + CREDIBILITY_PRIOR_OBS)
    out["reliability_composite"] = out["reliability_composite_raw"] * out["credibility_weight"]

    return out.sort_values("reliability_composite", ascending=False).reset_index(drop=True)


def simple_accuracy_leaderboard(raw_fe: pd.DataFrame, min_obs: int = 1) -> pd.DataFrame:
    """
    FALLBACK ranking for exactly the situation the first --live single-ticker
    run hits: analyst_reliability_scores() needs a trained NN per analyst
    (>=10 lagged observations, p.14), which one ticker's handful of quarters
    can't supply -- see fetch_live_ticker_data()'s "KNOWN LIMITATION" note.

    This computes 2 of the 3 composite components DIRECTLY from raw forecast
    errors, no model training required:
      accuracy_score    = -mean(|fe|)  (same definition as the full score)
      consistency_score = -std(fe)     (volatility of her RAW forecast error,
                                         not the NN-residual version -- since
                                         there's no NN to net out a systematic
                                         bias, this is a cruder proxy: an
                                         analyst who is consistently 2% too
                                         high every quarter scores the same
                                         as one who bounces between -2% and
                                         +2%; the full score in
                                         analyst_reliability_scores() tells
                                         those two apart, this one can't.)

    predictability_r2 is OMITTED entirely (not approximated) -- it's not
    meaningful without a trained model, and filling in a fake proxy would
    misrepresent what this number is.

    This is explicitly a SAME-DATA, SAME-DAY partial answer -- rank ONLY on
    the (firm, quarter)s you actually pulled, not a substitute for the full
    cross-firm reliability score. Requires >= min_obs forecast-error
    observations per analyst (default 1 -- i.e. include everyone who showed
    up at least once); raise it if you want only analysts covering multiple
    quarters.

    CREDIBILITY WEIGHTING (Patrick's request -- same fix as
    analyst_reliability_scores()): this is exactly the function that
    surfaced the problem -- in the first real AAPL run, an analyst with a
    SINGLE observation ranked #1 purely because that one quarter happened
    to be a great call. partial_reliability_score_raw is multiplied by
    n_observations / (n_observations + config.CREDIBILITY_PRIOR_OBS) to get
    the final partial_reliability_score (what this frame is ranked by) --
    thin-evidence analysts get shrunk toward 0 (neutral) rather than
    dominating the top of the table.

    FRESHNESS (Patrick's request -- see analyst_reliability_scores()'s
    docstring for the full rationale): when raw_fe has a 'staleness_days'
    column (every --live pull now writes one; older CSVs or --mock data from
    before this feature won't have it and the score just falls back to
    accuracy+consistency, unaffected), this also computes
    avg_staleness_days -> freshness_score = -avg_staleness_days -> its
    z-score, and folds it into partial_reliability_score_raw alongside
    accuracy/consistency. An analyst who lets her forecast go stale for
    weeks before the print scores worse here than one who keeps updating it
    right up to the event, even if their historical accuracy looks similar.
    """
    from src.config import CREDIBILITY_PRIOR_OBS
    has_staleness = "staleness_days" in raw_fe.columns
    rows = []
    for analyst, g in raw_fe.dropna(subset=["fe"]).groupby("analyst"):
        fe = g["fe"].values
        if len(fe) < min_obs:
            continue
        row = {
            "analyst": analyst,
            "n_observations": len(fe),
            "mean_fe": float(np.mean(fe)),
            "accuracy_score": -float(np.mean(np.abs(fe))),
            "consistency_score": -float(np.std(fe)) if len(fe) > 1 else np.nan,
        }
        if has_staleness:
            staleness_vals = g["staleness_days"].dropna()
            row["avg_staleness_days"] = float(staleness_vals.mean()) if len(staleness_vals) else np.nan
        row.update(_broker_fields(g))
        rows.append(row)

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    if has_staleness and "avg_staleness_days" in out.columns:
        out["freshness_score"] = -out["avg_staleness_days"]

    z_source_cols = ["accuracy_score", "consistency_score"]
    if "freshness_score" in out.columns:
        z_source_cols.append("freshness_score")
    for col in z_source_cols:
        mu, sigma = out[col].mean(), out[col].std()
        out[f"{col}_z"] = (out[col] - mu) / sigma if sigma and sigma > 0 else 0.0

    # NaN z-scores (consistency_score_z for single-observation analysts,
    # freshness_score_z if staleness was never resolvable for someone) are
    # skipped automatically by .mean(axis=1) -- no special-cased fallback
    # needed beyond that.
    z_cols = [f"{c}_z" for c in z_source_cols]
    out["partial_reliability_score_raw"] = out[z_cols].mean(axis=1)

    out["credibility_weight"] = out["n_observations"] / (out["n_observations"] + CREDIBILITY_PRIOR_OBS)
    out["partial_reliability_score"] = out["partial_reliability_score_raw"] * out["credibility_weight"]

    return out.sort_values("partial_reliability_score", ascending=False).reset_index(drop=True)


def factor_adjusted_partial_leaderboard(
    raw_fe: pd.DataFrame,
    factor_model: str = "FF3+MOM",
    min_factor_obs: "int | None" = None,
    min_obs: int = 1,
) -> pd.DataFrame:
    """
    Same idea as compute_factor_adjusted_scores(), but built on top of
    simple_accuracy_leaderboard() instead of analyst_reliability_scores().

    Why this exists: compute_factor_adjusted_scores()'s base score requires
    an analyst to have cleared the paper's NN-training bar (>=10 LAGGED
    observations, p.14) -- a much stricter requirement than factor-alpha's
    own bar (config.MIN_FACTOR_OBS quarterly observations, default 10, no
    lag needed). On a real multi-ticker pull spanning 2-3 years, almost no
    analyst clears the NN bar (most cover one firm, so there simply aren't
    10 PRIOR quarters before any of their target quarters yet) -- so
    compute_factor_adjusted_scores() ends up with 1 analyst even when 6+
    analysts have enough history for a real factor-alpha. This function
    starts from the same NN-independent base simple_accuracy_leaderboard()
    already uses for exactly this reason, and adds factor_alpha on top for
    whoever clears that separate bar -- so today's real data actually
    surfaces a factor-adjusted view, not just a placeholder for once you
    have 5+ years of history.

    Adds columns (same names/meaning as compute_factor_adjusted_scores(),
    so a caller doesn't need to branch on which one ran):
    factor_alpha, factor_alpha_tstat, factor_alpha_annualized, factor_alpha_z,
    loading_Mkt-RF, loading_SMB, loading_HML, loading_MOM (+RMW, CMA for FF5),
    partial_reliability_score_with_factor_raw, partial_reliability_score_with_factor.

    Analysts without enough quarterly history for factor_alpha keep their
    ordinary partial_reliability_score components; factor_alpha_z is simply
    excluded from their composite mean (same NaN-skipping pattern used
    everywhere else in this file for freshness_score_z).
    """
    base = simple_accuracy_leaderboard(raw_fe, min_obs=min_obs)
    if base.empty:
        return base

    # compute_analyst_factor_alpha() expects [analyst, firm, quarter, fe,
    # nn_predicted_fe] -- raw_fe has no NN predictions (this is the whole
    # point of using the partial base), so pass NaN and let the residual
    # fallback (fe - NaN.fillna(0) = fe itself) do the right thing: without
    # a trained model to net out a systematic bias, an analyst's raw
    # forecast error IS her un-explained residual.
    fe_for_factors = raw_fe[["analyst", "firm", "quarter", "fe"]].copy()
    fe_for_factors["nn_predicted_fe"] = np.nan

    factor_results = compute_analyst_factor_alpha(
        fe_for_factors, factor_model=factor_model, min_obs=min_factor_obs,
    )

    factor_cols_all = [
        "factor_alpha", "factor_alpha_tstat", "factor_alpha_annualized",
        "loading_Mkt-RF", "loading_SMB", "loading_HML", "loading_MOM",
        "loading_RMW", "loading_CMA", "n_obs", "r_squared",
    ]
    if factor_results.empty:
        for c in factor_cols_all:
            base[c] = np.nan
        base["factor_alpha_z"] = np.nan
        base["partial_reliability_score_with_factor_raw"] = base["partial_reliability_score_raw"]
        base["partial_reliability_score_with_factor"] = base["partial_reliability_score"]
        return base

    merged = base.set_index("analyst").join(
        factor_results.rename(columns={"n_obs": "n_factor_obs"}), how="left",
    ).reset_index()

    valid = merged["factor_alpha"].notna()
    merged["factor_alpha_z"] = np.nan
    if valid.any():
        mu, sigma = merged.loc[valid, "factor_alpha"].mean(), merged.loc[valid, "factor_alpha"].std()
        if sigma and sigma > 0:
            merged.loc[valid, "factor_alpha_z"] = (merged.loc[valid, "factor_alpha"] - mu) / sigma
        else:
            merged.loc[valid, "factor_alpha_z"] = 0.0

    z_source_cols = ["accuracy_score_z", "consistency_score_z"]
    if "freshness_score_z" in merged.columns:
        z_source_cols.append("freshness_score_z")
    if merged["factor_alpha_z"].notna().any():
        z_source_cols.append("factor_alpha_z")

    merged["partial_reliability_score_with_factor_raw"] = merged[z_source_cols].mean(axis=1)
    merged["partial_reliability_score_with_factor"] = (
        merged["partial_reliability_score_with_factor_raw"] * merged["credibility_weight"]
    )

    return merged.sort_values("partial_reliability_score_with_factor", ascending=False).reset_index(drop=True)


# =============================================================================
# Factor-Adjusted Analyst Scoring (NEW -- non-breaking augmentation)
# =============================================================================

def compute_factor_adjusted_scores(
    predicted: pd.DataFrame,
    factor_model: str = "FF3+MOM",
    min_factor_obs: "int | None" = None,
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

    Non-breaking: existing `reliability_composite` column is unchanged.
    Analysts with insufficient factor data have NaN factor columns;
    `factor_alpha_z` is excluded from z-mean automatically.
    """
    from src.config import CREDIBILITY_PRIOR_OBS, MIN_FACTOR_OBS

    # 1. Get base scores (unchanged)
    base_scores = analyst_reliability_scores(predicted)
    if base_scores.empty:
        return base_scores

    # 2. Compute factor alphas on the predicted panel
    # predicted has: analyst, firm, quarter, year, fe, nn_predicted_fe
    factor_results = compute_analyst_factor_alpha(
        predicted[["analyst", "firm", "quarter", "fe", "nn_predicted_fe"]],
        factor_model=factor_model,
        min_obs=min_factor_obs or MIN_FACTOR_OBS,
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


# =============================================================================
# 7. FACTSET DATA PULL (Formula API)  --  LIVE mode only
#
#   Reuses src/factset_client.py (already proven working on this account).
#   Every function here is a SEPARATE, explicit, single-ticker call so the
#   call budget (config.SESSION_CALL_BUDGET) stays easy to reason about --
#   see CallBudget below.
# =============================================================================

class CallBudget:
    """
    TWO layers of protection, both checked on every spend():

    1. THIS session's self-imposed cap (config.SESSION_CALL_BUDGET) -- an
       in-memory counter, resets every time you run the script. Patrick's
       own comfort ceiling, raised twice already by explicit choice.
    2. FactSet's REAL, server-enforced limits (src/api_usage_tracker.py) --
       persisted to disk across every run and every day, because the
       session cap alone can't see calls made in earlier runs. See that
       module's docstring for why the 100-requests/DAY ceiling is the
       actual binding constraint for this project's single-ticker call
       pattern, not the 100,000-points/month one.
    """
    def __init__(self, budget: int):
        self.budget = budget
        self.spent = 0

    def spend(self, n: int, label: str):
        from src.api_usage_tracker import check_and_record
        check_and_record(n_requests=n, n_points=n, label=label)  # raises if FactSet's real limits would be breached

        self.spent += n
        if self.spent > self.budget:
            raise RuntimeError(
                f"Call budget exceeded: tried to spend {n} more on '{label}', "
                f"total would be {self.spent}/{self.budget}. Stopping -- "
                f"raise config.SESSION_CALL_BUDGET if this was intentional."
            )
        print(f"[budget] +{n} ({label}) -- {self.spent}/{self.budget} used")


def fetch_live_ticker_data(
    ticker: str,
    budget: CallBudget,
    n_quarters: Optional[int] = None,
    return_raw: bool = False,
):
    """
    Pulls one ticker's full forecast-error input panel via the Formula API.

    By default it returns the same forecast-error DataFrame as before. When
    ``return_raw=True`` it returns a dict with that DataFrame plus separate,
    audit-ready raw tables for estimates, actuals, and prices. These tables
    are built from the SAME API responses already required by the model, so
    this stage adds 0 FactSet API calls and does not change the existing scoring
    calculations.

    Built on the confirmed-working formulas consolidated in
    src/factset_data.py (see that module's docstring for the exact evidence
    behind each one, and the full point-cost breakdown). For each of the
    last `n_quarters` (config.LIVE_N_QUARTERS by default):

      1. actual reported EPS + report date          (1 shared call, all quarters)
      2. price + market cap 10 trading days prior    (1 call PER quarter)
      3. every contributing analyst's EPS estimate,
         snapshotted just before the print            (1 call PER quarter)

    Total cost: 1 + 2*n_quarters API points for this one ticker.

    SIC CODE (for FF48 industry aggregation): checks config.TICKER_SIC_OVERRIDES
    first (manual, always wins if present), then falls back to
    src.sic_lookup.get_sic_code() -- an AUTOMATIC lookup against SEC EDGAR's
    free public JSON endpoints (ticker -> CIK -> SIC), cached to disk after
    the first call. 0 FactSet API points either way -- this is a completely
    separate data source from FactSet, so it never touches CallBudget. Only
    falls back to the 'UNMAPPED'/manual-entry path if SEC EDGAR has no record
    for this ticker (e.g. non-US-domestic filers) or the network is
    unreachable.

    KNOWN LIMITATION (documented, not hidden): the paper's per-analyst NN
    model (Eq. 2) is trained on an analyst's forecast errors ACROSS EVERY
    FIRM she covers, and needs >=10 historical observations (p.14). A
    single-ticker pull like this one can supply at most `n_quarters`
    observations per analyst for ONE firm -- fewer than 10 unless you raise
    n_quarters well past what's realistic for one company's earnings
    history. That means analyst_reliability_scores() on this data is a
    CODE-PATH validation (does the math run correctly end-to-end on real
    numbers?), not yet a meaningful reliability signal -- for that, repeat
    this same call sequence across a real multi-ticker universe (see
    README.md's "Next steps") so each analyst's history spans many firms.
    """
    from src.config import LIVE_N_QUARTERS, TRADING_DAYS_BEFORE_EARNINGS, \
        ANALYST_SNAPSHOT_DAYS_BEFORE_EARNINGS, TICKER_SIC_OVERRIDES
    from src.factset_data import (
        get_quarterly_eps_history,
        get_price_and_mktcap_on_date,
        get_analyst_eps_snapshot,
        trading_days_before,
    )

    n_quarters = n_quarters or LIVE_N_QUARTERS
    sic_code = TICKER_SIC_OVERRIDES.get(ticker)
    if sic_code is not None:
        print(f"[fetch_live_ticker_data] SIC code for {ticker}: {sic_code} "
              f"(manual override, config.TICKER_SIC_OVERRIDES)")
    else:
        from src.sic_lookup import get_sic_code
        lookup = get_sic_code(ticker)
        sic_code = lookup["sic_code"]
        if sic_code is not None:
            print(f"[fetch_live_ticker_data] SIC code for {ticker}: {sic_code} "
                  f"({lookup['sic_description']}) -- auto-resolved via SEC EDGAR "
                  f"[{lookup['source']}], 0 FactSet API calls")
        else:
            print(f"[fetch_live_ticker_data] WARNING: no SIC code on file for {ticker} "
                  f"and SEC EDGAR auto-lookup couldn't resolve one either "
                  f"({lookup['source']}) -- industry aggregation will show 'UNMAPPED'. "
                  f"Add a manual entry to config.TICKER_SIC_OVERRIDES if you have one.")

    budget.spend(1, f"quarterly EPS history x{n_quarters} ({ticker})")
    eps_history = get_quarterly_eps_history(ticker, n_quarters=n_quarters)
    if eps_history.empty:
        raise RuntimeError(f"No quarterly EPS history returned for {ticker} -- check credentials/ticker.")
    print(f"[fetch_live_ticker_data] {len(eps_history)} quarters of actual EPS for {ticker}:")
    print(eps_history.to_string(index=False))

    all_rows = []
    raw_actual_rows = []
    raw_price_rows = []
    raw_estimate_rows = []

    for _, q in eps_history.iterrows():
        quarter_end = q["quarter_end"]
        report_date = q["report_date"]
        actual_eps = q["actual_eps"]
        if not report_date or actual_eps is None:
            print(f"[fetch_live_ticker_data] skipping {quarter_end}: missing report_date or actual_eps")
            continue

        price_date = trading_days_before(report_date, TRADING_DAYS_BEFORE_EARNINGS)
        budget.spend(1, f"price+mktcap {quarter_end} ({ticker})")
        price_info = get_price_and_mktcap_on_date(ticker, price_date)

        quarter_label = f"{quarter_end[:4]}Q{(int(quarter_end[5:7]) - 1) // 3 + 1}"
        year = int(quarter_end[:4])

        raw_actual_rows.append(
            {
                "firm": ticker,
                "quarter_end": quarter_end,
                "quarter": quarter_label,
                "year": year,
                "report_date": report_date,
                "actual_eps": actual_eps,
                "sic_code": sic_code,
            }
        )
        raw_price_rows.append(
            {
                "firm": ticker,
                "quarter_end": quarter_end,
                "quarter": quarter_label,
                "year": year,
                "report_date": report_date,
                "price_date": price_date,
                "price_10d_prior": price_info.get("price"),
                "market_cap": price_info.get("market_cap"),
                "market_cap_direct": price_info.get("mkt_val_direct"),
                "shares_outstanding": price_info.get("shares_out"),
            }
        )

        if price_info["market_cap"] is None:
            print(f"[fetch_live_ticker_data] WARNING: no market cap resolved for {ticker} on {price_date} "
                  f"(quarter {quarter_end}) -- this quarter will be dropped from weighting.")

        snapshot_date = trading_days_before(report_date, ANALYST_SNAPSHOT_DAYS_BEFORE_EARNINGS)
        budget.spend(1, f"analyst EPS snapshot {quarter_end} ({ticker})")
        analyst_snap = get_analyst_eps_snapshot(ticker, snapshot_date)
        print(f"[fetch_live_ticker_data] {quarter_end}: {len(analyst_snap)} contributing analysts "
              f"as of {snapshot_date}")

        for _, a in analyst_snap.iterrows():
            revision_date = a["revision_date"]

            # Preserve EVERY contributing estimate row in the raw audit table,
            # even when it cannot be converted into a forecast error because the
            # estimate or price is missing. This is deliberately more complete
            # than the modelling table below.
            staleness_days = None
            if revision_date:
                try:
                    rd = datetime.strptime(str(int(revision_date)), "%Y%m%d").date()
                    sd = datetime.strptime(str(snapshot_date), "%Y%m%d").date()
                    staleness_days = max(0, (sd - rd).days)
                except (ValueError, TypeError):
                    staleness_days = None

            raw_estimate_rows.append(
                {
                    "firm": ticker,
                    "quarter_end": quarter_end,
                    "quarter": quarter_label,
                    "year": year,
                    "report_date": report_date,
                    "snapshot_date": snapshot_date,
                    "analyst": a["analyst"],
                    "broker": a["broker"],
                    "broker_code": a["broker_code"],
                    "estimate_value": a["est_value"],
                    "revision_date": revision_date,
                    "staleness_days": staleness_days,
                    "actual_eps": actual_eps,
                    "price_10d_prior": price_info.get("price"),
                    "market_cap": price_info.get("market_cap"),
                }
            )

            if a["est_value"] is None or price_info["price"] is None:
                continue
            fe = forecast_error(a["est_value"], actual_eps, price_info["price"])

            # STALENESS (Patrick's request): how many calendar days before the
            # snapshot date did this analyst last actually TOUCH this estimate
            # (FactSet's MODDATEN)? 0 = she updated it the same day we
            # snapshotted; large = she hadn't revisited her number in a while,
            # so it's less likely to reflect anything she learned close to the
            # print. See analyst_reliability_scores()'s docstring for how this
            # becomes a "freshness" score component. Calendar days, not
            # trading days, for simplicity -- a documented approximation
            # (weekend noise affects every analyst roughly equally, so it
            # doesn't distort relative ranking).
            all_rows.append(
                {
                    "analyst": a["analyst"],
                    "broker": a["broker"],            # her brokerage AT THIS SNAPSHOT DATE -- see
                                                       # _broker_fields()'s docstring for why this is
                                                       # tracked per-quarter, not as a fixed label
                    "broker_code": a["broker_code"],  # stable numeric ID for the same broker --
                                                       # see get_analyst_eps_snapshot()'s docstring
                    "firm": ticker,
                    "quarter": quarter_label,
                    "year": year,
                    "fe": fe,
                    "market_cap": price_info["market_cap"],
                    "sic_code": sic_code,
                    "revision_date": revision_date,
                    "staleness_days": staleness_days,
                }
            )

    df = pd.DataFrame(all_rows)
    raw_actuals = pd.DataFrame(raw_actual_rows)
    raw_prices = pd.DataFrame(raw_price_rows)
    raw_estimates = pd.DataFrame(raw_estimate_rows)

    print(f"[fetch_live_ticker_data] built {len(df)} (analyst, quarter) forecast-error rows for {ticker} "
          f"({df['analyst'].nunique() if not df.empty else 0} distinct analysts)")

    if not return_raw:
        return df

    return {
        "raw_forecast_errors": df,
        "raw_estimates": raw_estimates,
        "raw_actuals": raw_actuals,
        "raw_prices": raw_prices,
    }


# =============================================================================
# 8. ORCHESTRATION / CLI
# =============================================================================

def _make_mock_data(n_analysts=6, n_firms=3, n_years=6, seed=42) -> pd.DataFrame:
    """
    Synthetic (analyst, firm, quarter) forecast-error panel so every formula
    above can be verified end-to-end without any API calls. Each analyst has
    her own persistent bias + noise, and firms have SIC codes across a
    couple of FF48 industries so industry aggregation is testable too.

    Also assigns each analyst a broker NAME and CODE (so --mock exercises
    the same current_broker / current_broker_code / n_brokers /
    filter_by_broker() machinery --live does) -- ANALYST_0 is scripted to
    switch employers halfway through the window specifically so n_brokers
    > 1 gets tested here too, not just in theory.

    Also gives each analyst a persistent "staleness_tendency" (some analysts
    consistently update their estimate right before the print, others let it
    sit for weeks) so the freshness signal in analyst_reliability_scores() /
    simple_accuracy_leaderboard() has something real to rank on here too, not
    just in --live data.
    """
    rng = np.random.default_rng(seed)
    sic_pool = [3571, 3576, 2820]  # Computers, Computers, Chemicals -> 2 FF48 industries
    sics = [sic_pool[i % len(sic_pool)] for i in range(n_firms)]
    firm_sics = {f"FIRM{i}": sics[i] for i in range(n_firms)}
    firms = list(firm_sics.keys())
    quarters = [f"{y}Q{q}" for y in range(2015, 2015 + n_years) for q in range(1, 5)]

    # (name, code) pairs -- codes are arbitrary but stable, matching the shape
    # BKR_CODE actually returns (a mix of small and large integers).
    broker_pool = [
        ("Alpha Capital", 1001), ("Beacon Securities", 1002), ("Crestview Partners", 1003),
        ("Dover & Co", 1004), ("Elm Street Research", 1005), ("Fenwick Bank", 1006),
    ]
    switch_year = 2015 + n_years // 2  # ANALYST_0 changes brokers at this year
    growth_street = ("Growth Street Research", 9999)

    rows = []
    for a in range(n_analysts):
        analyst_id = f"ANALYST_{a}"
        bias = rng.normal(0, 0.01)          # her persistent, learnable bias
        noise_scale = rng.uniform(0.005, 0.02)
        home_broker_name, home_broker_code = broker_pool[a % len(broker_pool)]
        staleness_tendency = rng.uniform(0, 15)  # her typical days-since-last-update
        for firm in firms:
            market_cap = rng.uniform(5e9, 2e12)
            for q in quarters:
                year = int(q[:4])
                fe = bias + rng.normal(0, noise_scale)
                broker_name, broker_code = (
                    growth_street if (a == 0 and year >= switch_year)
                    else (home_broker_name, home_broker_code)
                )
                staleness_days = max(0, int(round(rng.normal(staleness_tendency, 3))))
                rows.append(
                    {
                        "analyst": analyst_id,
                        "broker": broker_name,
                        "broker_code": broker_code,
                        "firm": firm,
                        "quarter": q,
                        "year": year,
                        "fe": fe,
                        "market_cap": market_cap,
                        "sic_code": firm_sics[firm],
                        "staleness_days": staleness_days,
                    }
                )
    return pd.DataFrame(rows)


def run_pipeline(
    raw_fe: pd.DataFrame,
    factor_model: str = "FF3+MOM",
    n_per_leg: int = 5,
    run_factor_backtest: bool = True,
) -> dict:
    """
    raw_fe columns required: [analyst, firm, quarter, year, fe, market_cap, sic_code]
    Runs sections 2-7 end to end. Returns a dict of the key output frames.
    """
    print(f"[1] input panel: {len(raw_fe)} (analyst, firm, quarter) rows, "
          f"{raw_fe['analyst'].nunique()} analysts, {raw_fe['firm'].nunique()} firms")

    predicted = run_expanding_window(raw_fe)
    print(f"[2] expanding-window NN/linear predictions: "
          f"{predicted['nn_predicted_fe'].notna().sum()} rows with a valid NN prediction")

    lo, hi = predicted["fe"].quantile(0.01), predicted["fe"].quantile(0.99)
    predicted["fe"] = winsorize_with_thresholds(predicted["fe"], lo, hi)
    predicted["nn_predicted_fe"] = winsorize_with_thresholds(predicted["nn_predicted_fe"], lo, hi)
    predicted["linear_predicted_fe"] = winsorize_with_thresholds(predicted["linear_predicted_fe"], lo, hi)
    print(f"[3] winsorized actual FE at [{lo:.4f}, {hi:.4f}] (1%/99%), same cutoffs applied to predictions")

    consensus = to_consensus(predicted)
    consensus = add_ff48_industry(consensus)
    print(f"[4a] consensus (firm, quarter) rows: {len(consensus)}, "
          f"industries seen: {sorted(consensus['industry'].unique())}")

    industry = industry_level(consensus)
    market = market_level(consensus)
    print(f"[4b] industry-quarter rows: {len(industry)}; market-quarter rows: {len(market)}")

    scores = analyst_reliability_scores(predicted)
    print(f"[6] analyst reliability scores computed for {len(scores)} analysts")

    # Section 7: Factor-model risk adjustment for Long-Short strategy
    factor_backtest = None
    if run_factor_backtest:
        print("[7] running factor-model risk adjustment backtest...")
        factor_backtest = run_factor_bt(
            industry_sentiment=industry,
            factor_model_name=factor_model,
            n_per_leg=n_per_leg,
        )
        if factor_backtest:
            res = factor_backtest.regression
            print(f"    Model: {factor_backtest.model_name}")
            print(f"    Alpha (monthly): {res.alpha:+.4f}% (t={res.alpha_tstat:+.2f}, annualized={res.alpha_annualized:+.2f}%)")
            print(f"    Loadings: " + ", ".join(f"{k}={v:+.3f} (t={res.loadings_tstats.get(k, float('nan')):+.2f})" for k, v in res.loadings.items()))
            print(f"    R²={res.r_squared:.3f}, n={res.n_obs}, Sharpe (ann)={res.sharpe_annualized:.2f}")
            print(f"    Period: {factor_backtest.start_date.date()} to {factor_backtest.end_date.date()} ({factor_backtest.n_months} months)")
        else:
            print("    WARNING: factor backtest could not run (insufficient data)")

    return {
        "predicted": predicted,
        "consensus": consensus,
        "industry": industry,
        "market": market,
        "analyst_scores": scores,
        "factor_backtest": factor_backtest,
    }



def load_existing_output_panel(tickers: list[str], outputs_dir: str = "outputs") -> dict:
    """Load already-created per-ticker CSV outputs and combine them into one
    multi-company panel without making any FactSet/API calls.

    Required per ticker: live_<ticker>_raw_forecast_errors.csv
    Optional per ticker: raw_estimates.csv, raw_actuals.csv, raw_prices.csv
    """
    if not tickers:
        raise ValueError("No tickers supplied. Pass --tickers AAPL-US,CME-US,...")

    os.makedirs(outputs_dir, exist_ok=True)
    required = ["analyst", "firm", "quarter", "year", "fe", "market_cap", "sic_code"]
    combined = {}
    missing = []

    for ticker in tickers:
        safe = ticker.replace("-", "_")
        path = os.path.join(outputs_dir, f"live_{safe}_raw_forecast_errors.csv")
        if not os.path.exists(path):
            missing.append(path)
            continue
        df = pd.read_csv(path)
        if df.empty:
            print(f"[outputs] WARNING: {path} is empty -- skipped")
            continue
        for col in required:
            if col not in df.columns:
                raise ValueError(f"{path} is missing required column '{col}'")
        # Keep the filename's ticker as an audit field; do not rewrite firm unless missing.
        if "source_ticker" not in df.columns:
            df["source_ticker"] = ticker
        combined.setdefault("raw_forecast_errors", []).append(df)

        for key in ("raw_estimates", "raw_actuals", "raw_prices"):
            optional_path = os.path.join(outputs_dir, f"live_{safe}_{key.replace('_', '_')}.csv")
            if os.path.exists(optional_path):
                odf = pd.read_csv(optional_path)
                if not odf.empty:
                    if "source_ticker" not in odf.columns:
                        odf["source_ticker"] = ticker
                    combined.setdefault(key, []).append(odf)

    if missing:
        raise FileNotFoundError(
            "Missing existing raw forecast-error CSVs for: " + ", ".join(missing) +
            " . This mode never calls FactSet; create those company outputs first."
        )

    if not combined.get("raw_forecast_errors"):
        raise ValueError("No non-empty raw forecast-error CSVs were found in outputs/")

    out = {}
    out["raw_forecast_errors"] = pd.concat(combined["raw_forecast_errors"], ignore_index=True)
    for key in ("raw_estimates", "raw_actuals", "raw_prices"):
        frames = combined.get(key, [])
        out[key] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    # Guard against accidental duplicate rows when the same ticker CSV is listed twice.
    out["raw_forecast_errors"] = out["raw_forecast_errors"].drop_duplicates().reset_index(drop=True)
    for key in ("raw_estimates", "raw_actuals", "raw_prices"):
        if not out[key].empty:
            out[key] = out[key].drop_duplicates().reset_index(drop=True)

    print(
        f"[outputs] loaded {len(out['raw_forecast_errors']):,} forecast-error rows "
        f"from {out['raw_forecast_errors']['firm'].nunique()} companies "
        f"and {out['raw_forecast_errors']['sic_code'].nunique()} SIC codes; 0 FactSet calls"
    )
    return out


def _run_and_write_factor_adjusted(prefix: str, raw_fe: pd.DataFrame, predicted: pd.DataFrame,
                                    factor_model: str, min_factor_obs: "int | None", label: str) -> None:
    """
    Shared by --mock/--live/--from-outputs's `if args.factor_adjusted:` blocks
    so the two-tier logic (full NN-gated score + practical partial-leaderboard
    fallback) only has to be written, and kept correct, once.

    Writes BOTH outputs/<prefix>_factor_adjusted_scores.csv (NN-gated, meant
    for a large multi-year universe -- see compute_factor_adjusted_scores()'s
    docstring) and outputs/<prefix>_partial_leaderboard_factor_adjusted.csv
    (NN-independent, meant to actually surface something on today's data --
    see factor_adjusted_partial_leaderboard()'s docstring for why these two
    can disagree sharply on analyst COUNT even though they're asking the same
    "does she just ride a risk factor" question).
    """
    print(f"\n--- Computing factor-adjusted analyst scores {label} ---")

    full = compute_factor_adjusted_scores(predicted, factor_model=factor_model, min_factor_obs=min_factor_obs)
    n_full_alpha = int(full["factor_alpha"].notna().sum()) if "factor_alpha" in full.columns else 0
    print(f"Full (NN-gated) score: {len(full)} analysts total, {n_full_alpha} with a real factor_alpha")
    full.to_csv(f"outputs/{prefix}_factor_adjusted_scores.csv", index=False)

    partial = factor_adjusted_partial_leaderboard(raw_fe, factor_model=factor_model, min_factor_obs=min_factor_obs)
    n_partial_alpha = int(partial["factor_alpha"].notna().sum()) if "factor_alpha" in partial.columns else 0
    print(f"Partial (NN-independent) leaderboard: {len(partial)} analysts total, "
          f"{n_partial_alpha} with a real factor_alpha")
    partial.to_csv(f"outputs/{prefix}_partial_leaderboard_factor_adjusted.csv", index=False)

    print(f"Wrote outputs/{prefix}_factor_adjusted_scores.csv "
          f"and outputs/{prefix}_partial_leaderboard_factor_adjusted.csv")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="Run on synthetic data (0 API calls).")
    parser.add_argument("--live", action="store_true", help="Run on real FactSet data (uses call budget).")
    parser.add_argument("--ticker", default=None, help="Single ticker for --live mode, e.g. AAPL-US")
    parser.add_argument(
        "--from-outputs", action="store_true",
        help="Build a multi-company panel entirely from existing outputs/live_*_raw_*.csv files. "
             "Makes 0 FactSet/API calls; requires --tickers.",
    )
    parser.add_argument(
        "--tickers", default=None,
        help="Comma-separated ticker list for --from-outputs, e.g. AAPL-US,CME-US,TSM-US.",
    )
    parser.add_argument(
        "--quarters", type=int, default=None,
        help="How many trailing quarters of actual EPS/analyst estimates to pull for --live mode "
             "(overrides config.LIVE_N_QUARTERS for this run only). Cost = 1 + 2*quarters API "
             "points/requests -- e.g. --quarters 28 costs 57. Defaults to config.LIVE_N_QUARTERS (12) "
             "if omitted.",
    )
    parser.add_argument(
        "--broker", default=None,
        help="Filter analyst-level score tables to analysts CURRENTLY at one or more brokerages. "
             "Comma-separated for multiple, e.g. --broker \"morgan,jpmorgan\". Case-insensitive "
             "substring match on the broker NAME by default. Use --broker-by code with numeric "
             "FactSet broker codes for an exact, more reliable match (e.g. --broker 2295,1080 "
             "--broker-by code) -- see filter_by_broker()'s docstring for why code beats name "
             "when you're selecting several brokers at once. Doesn't affect industry/market "
             "aggregation, which always uses every analyst.",
    )
    parser.add_argument(
        "--broker-by", default="name", choices=["name", "code"],
        help="Match --broker against the broker NAME (substring, default) or broker CODE (exact).",
    )
    # Section 7: Factor-model risk adjustment CLI flags
    parser.add_argument(
        "--factor-model", default="FF3+MOM", choices=["FF3+MOM", "FF5", "FF5+MOM"],
        help="Factor model for Long-Short risk adjustment (Section 7). "
             "FF3+MOM = Carhart 4-factor (Mkt-RF, SMB, HML, MOM) -- matches paper's "
             "'four-factor alpha 0.62%, t=2.48'. FF5 = pure Fama-French 5-factor "
             "(Mkt-RF, SMB, HML, RMW, CMA). FF5+MOM adds Momentum to FF5.",
    )
    parser.add_argument(
        "--factor-backtest", action="store_true",
        help="Run the factor-model risk adjustment backtest (Section 7) on the "
             "Long-Short industry strategy. Uses --factor-model. Requires industry "
             "sentiment data (from --mock or --live) and Ken French factor/industry "
             "data (0 FactSet calls, auto-downloaded on first use).",
    )
    parser.add_argument(
        "--k-per-leg", type=int, default=5,
        help="Number of industries per leg in Long-Short sort (paper K=5). "
             "Applies to factor backtest.",
    )
    # Factor-adjusted analyst scoring (NEW)
    parser.add_argument(
        "--factor-adjusted", action="store_true",
        help="Compute factor-adjusted analyst scores (adds a factor-alpha component "
             "to the reliability composite -- the part of an analyst's forecast-error "
             "residual that ISN'T explained by market/size/value/momentum risk factors, "
             "so an analyst who just happens to be tilted toward a factor that did well "
             "doesn't get mistaken for one who's actually good at forecasting). Uses "
             "--factor-model. Works with --mock, --live, and --from-outputs. Requires "
             "config.MIN_FACTOR_OBS (default 10) real QUARTERLY observations per analyst "
             "-- override per-run with --min-factor-obs. Writes "
             "outputs/<prefix>_factor_adjusted_scores.csv.",
    )
    parser.add_argument(
        "--min-factor-obs", type=int, default=None,
        help="Minimum quarterly observations required before an analyst gets a real "
             "factor-alpha (overrides config.MIN_FACTOR_OBS for this run). Lower = more "
             "analysts covered but noisier per-analyst regressions (fewer degrees of "
             "freedom); higher = fewer analysts covered but more statistically stable.",
    )
    args = parser.parse_args()

    if not args.mock and not args.live and not args.from_outputs:
        args.mock = True  # safe default: never spend API calls unless explicitly asked

    def _parse_broker_arg(raw: str, by: str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return [int(p) for p in parts] if by == "code" else parts

    if args.from_outputs:
        if args.mock or args.live:
            parser.error("--from-outputs cannot be combined with --mock or --live")
        if not args.tickers:
            parser.error("--from-outputs requires --tickers AAPL-US,CME-US,...")

        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        print("=== EXISTING OUTPUTS MODE: 0 FactSet/API calls ===")
        print(f"Tickers: {', '.join(tickers)}")

        existing = load_existing_output_panel(tickers)
        raw_fe = existing["raw_forecast_errors"]

        results = run_pipeline(
            raw_fe,
            factor_model=args.factor_model,
            n_per_leg=args.k_per_leg,
            run_factor_backtest=args.factor_backtest,
        )

        os.makedirs("outputs", exist_ok=True)
        pd.DataFrame([{
            "mode": "from_outputs",
            "tickers": ",".join(tickers),
            "n_companies": raw_fe["firm"].nunique(),
            "n_observations": len(raw_fe),
            "n_analysts": raw_fe["analyst"].nunique(),
            "n_industries": raw_fe["sic_code"].nunique(),
            "factor_model": args.factor_model,
            "run_timestamp": datetime.now().isoformat(timespec="seconds"),
            "factset_api_calls": 0,
        }]).to_csv("outputs/master_run_info.csv", index=False)

        raw_fe.to_csv("outputs/master_raw_forecast_errors.csv", index=False)
        if not existing["raw_estimates"].empty:
            existing["raw_estimates"].to_csv("outputs/master_raw_estimates.csv", index=False)
        if not existing["raw_actuals"].empty:
            existing["raw_actuals"].to_csv("outputs/master_raw_actuals.csv", index=False)
        if not existing["raw_prices"].empty:
            existing["raw_prices"].to_csv("outputs/master_raw_prices.csv", index=False)
        results["consensus"].to_csv("outputs/master_consensus.csv", index=False)
        results["industry"].to_csv("outputs/master_industry_sentiment.csv", index=False)
        results["analyst_scores"].to_csv("outputs/master_analyst_scores.csv", index=False)

        factor_bt = results.get("factor_backtest")
        if factor_bt is not None:
            factor_bt.factor_model.factors.reset_index().to_csv(
                "outputs/master_ff_factors.csv", index=False
            )
            factor_bt.ls_returns.rename("strategy_excess_return").reset_index().to_csv(
                "outputs/master_strategy_returns.csv", index=False
            )
            reg_rows = factor_bt.regression.summary_dict()
            pd.DataFrame(reg_rows, columns=["metric", "value"]).to_csv(
                "outputs/master_factor_regression.csv", index=False
            )
            print("Wrote master FF5/strategy outputs to outputs/master_*.csv")
        else:
            print("No factor backtest result was produced. Need enough industry coverage and overlapping returns.")

        # Factor-adjusted analyst scoring (NEW -- non-breaking; previously only
        # wired for --mock and --live, not --from-outputs, even though this is
        # the mode that actually pools enough analyst history across tickers
        # to make it meaningful)
        if args.factor_adjusted:
            _run_and_write_factor_adjusted(
                "master", raw_fe, results["predicted"], args.factor_model, args.min_factor_obs,
                label=f"for {', '.join(tickers)}",
            )

        print(
            f"\nMaster panel complete: {raw_fe['firm'].nunique()} companies, "
            f"{raw_fe['sic_code'].nunique()} SIC codes, {len(raw_fe):,} observations, "
            "0 FactSet/API calls."
        )

    if args.mock:
        print("=== MOCK MODE (synthetic data, 0 API calls) ===\n")
        raw_fe = _make_mock_data()
        do_backtest = args.factor_backtest or True  # default to running backtest in mock for testing
        results = run_pipeline(
            raw_fe,
            factor_model=args.factor_model,
            n_per_leg=args.k_per_leg,
            run_factor_backtest=do_backtest,
        )

        print("\n--- Top 3 most reliable analysts (mock data) ---")
        print(results["analyst_scores"].head(3).to_string(index=False))

        os.makedirs("outputs", exist_ok=True)
        results["analyst_scores"].to_csv("outputs/mock_analyst_scores.csv", index=False)
        results["industry"].to_csv("outputs/mock_industry_sentiment.csv", index=False)
        print("\nWrote outputs/mock_analyst_scores.csv and outputs/mock_industry_sentiment.csv")

        # Factor-adjusted analyst scoring (NEW -- non-breaking)
        if args.factor_adjusted:
            _run_and_write_factor_adjusted(
                "mock", raw_fe, results["predicted"], args.factor_model, args.min_factor_obs,
                label="(mock data)",
            )

        if args.broker:
            broker_values = _parse_broker_arg(args.broker, args.broker_by)
            filtered = filter_by_broker(results["analyst_scores"], broker_values, by=args.broker_by)
            print(f"\n--- Filtered to brokers matching {broker_values} (by {args.broker_by}): "
                  f"{len(filtered)} analyst(s) ---")
            print(filtered.to_string(index=False) if not filtered.empty else "(no matches)")
            slug = args.broker.lower().replace(" ", "_").replace(",", "-")
            filtered.to_csv(f"outputs/mock_analyst_scores_broker_{slug}.csv", index=False)

    if args.live:
        from src.config import SESSION_CALL_BUDGET, LIVE_N_QUARTERS

        if not args.ticker:
            parser.error("--live requires --ticker, e.g. --ticker AAPL-US")
        q_label = f"{args.quarters} quarters (override)" if args.quarters else "config.LIVE_N_QUARTERS (default)"
        print(f"=== LIVE MODE: {args.ticker}, {q_label}, budget {SESSION_CALL_BUDGET} ===\n")
        budget = CallBudget(SESSION_CALL_BUDGET)

        live_data = fetch_live_ticker_data(
            args.ticker,
            budget,
            n_quarters=args.quarters,
            return_raw=True,
        )
        raw_fe = live_data["raw_forecast_errors"]
        if raw_fe.empty:
            print("\nNo usable (analyst, quarter) rows were built -- nothing to score. "
                  "Check the warnings printed above (missing report dates, no analyst "
                  "coverage, no market cap, etc.).")
            return

        results = run_pipeline(
            raw_fe,
            factor_model=args.factor_model,
            n_per_leg=args.k_per_leg,
            run_factor_backtest=args.factor_backtest,
        )

        os.makedirs("outputs", exist_ok=True)
        safe_ticker = args.ticker.replace("-", "_")

        # Run metadata -- so the Excel workbook can show a coworker exactly what
        # produced it (ticker, quarter depth, date range covered, when it ran)
        # without needing Python or the terminal. export_to_excel.py reads this
        # into a "Run Info" sheet.
        run_meta = pd.DataFrame([{
            "ticker": args.ticker,
            "quarters_requested": args.quarters if args.quarters else LIVE_N_QUARTERS,
            "quarters_pulled": raw_fe["quarter"].nunique() if not raw_fe.empty else 0,
            "earliest_quarter": sorted(raw_fe["quarter"].unique())[0] if not raw_fe.empty else "",
            "latest_quarter": sorted(raw_fe["quarter"].unique())[-1] if not raw_fe.empty else "",
            "n_analysts": raw_fe["analyst"].nunique() if not raw_fe.empty else 0,
            "n_observations": len(raw_fe),
            "run_timestamp": datetime.now().isoformat(timespec="seconds"),
        }])
        run_meta.to_csv(f"outputs/live_{safe_ticker}_run_info.csv", index=False)

        raw_fe.to_csv(f"outputs/live_{safe_ticker}_raw_forecast_errors.csv", index=False)
        live_data["raw_estimates"].to_csv(f"outputs/live_{safe_ticker}_raw_estimates.csv", index=False)
        live_data["raw_actuals"].to_csv(f"outputs/live_{safe_ticker}_raw_actuals.csv", index=False)
        live_data["raw_prices"].to_csv(f"outputs/live_{safe_ticker}_raw_prices.csv", index=False)
        results["consensus"].to_csv(f"outputs/live_{safe_ticker}_consensus.csv", index=False)
        results["industry"].to_csv(f"outputs/live_{safe_ticker}_industry_sentiment.csv", index=False)

        # Factor strategy exports: keep the calculated strategy return series,
        # factor panel, and regression summary available to the Excel exporter.
        factor_bt = results.get("factor_backtest")
        if factor_bt is not None:
            factor_bt.factor_model.factors.reset_index().to_csv(
                f"outputs/live_{safe_ticker}_ff_factors.csv", index=False
            )
            factor_bt.ls_returns.rename("strategy_excess_return").reset_index().to_csv(
                f"outputs/live_{safe_ticker}_strategy_returns.csv", index=False
            )
            reg_rows = factor_bt.regression.summary_dict()
            pd.DataFrame(reg_rows, columns=["metric", "value"]).to_csv(
                f"outputs/live_{safe_ticker}_factor_regression.csv", index=False
            )

        print(f"\n--- Full (paper-consistent, NN-based) reliability scores for {args.ticker} ---")
        if results["analyst_scores"].empty:
            print("(empty -- see the KNOWN LIMITATION note in fetch_live_ticker_data's "
                  "docstring: a single ticker rarely gives any one analyst >=10 lagged "
                  "observations, so most/all analysts get dropped before NN training. "
                  "Falling back to a same-data partial score below -- see "
                  "simple_accuracy_leaderboard()'s docstring for exactly what it does "
                  "and doesn't capture vs. the full score.)")
        else:
            print(results["analyst_scores"].to_string(index=False))
        results["analyst_scores"].to_csv(f"outputs/live_{safe_ticker}_analyst_scores.csv", index=False)

        print(f"\n--- PARTIAL accuracy/consistency leaderboard for {args.ticker} "
              f"(same data, 0 extra API calls -- accuracy + consistency only, no NN) ---")
        fallback = simple_accuracy_leaderboard(raw_fe)
        if fallback.empty:
            print("(empty -- no analyst had a usable forecast error at all this run.)")
        else:
            print(fallback.head(15).to_string(index=False))
        fallback.to_csv(f"outputs/live_{safe_ticker}_partial_leaderboard.csv", index=False)

        if args.broker:
            broker_values = _parse_broker_arg(args.broker, args.broker_by)
            slug = args.broker.lower().replace(" ", "_").replace(",", "-")
            filtered_full = filter_by_broker(results["analyst_scores"], broker_values, by=args.broker_by)
            filtered_fallback = filter_by_broker(fallback, broker_values, by=args.broker_by)
            print(f"\n--- Filtered to brokers matching {broker_values} (by {args.broker_by}) ---")
            print(f"Full NN-based scores: {len(filtered_full)} analyst(s)")
            if not filtered_full.empty:
                print(filtered_full.to_string(index=False))
            print(f"Partial leaderboard: {len(filtered_fallback)} analyst(s)")
            if not filtered_fallback.empty:
                print(filtered_fallback.to_string(index=False))
            filtered_full.to_csv(f"outputs/live_{safe_ticker}_analyst_scores_broker_{slug}.csv", index=False)
            filtered_fallback.to_csv(f"outputs/live_{safe_ticker}_partial_leaderboard_broker_{slug}.csv", index=False)

        # Factor-adjusted analyst scoring (NEW -- non-breaking)
        if args.factor_adjusted:
            _run_and_write_factor_adjusted(
                f"live_{safe_ticker}", raw_fe, results["predicted"], args.factor_model, args.min_factor_obs,
                label=f"for {args.ticker}",
            )

        print(f"\nWrote outputs/live_{safe_ticker}_*.csv "
              f"(budget used: {budget.spent}/{budget.budget})")


if __name__ == "__main__":
    main()
