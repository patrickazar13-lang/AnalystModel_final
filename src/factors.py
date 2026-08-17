"""
Fama-French factor data + industry Long-Short strategy backtest.

Loads Ken French's free factor and industry-portfolio data (0 FactSet calls),
builds the paper's industry Long-Short portfolio returns from industry-level
analyst sentiment, and regresses them on a factor model (Carhart 4-factor
FF3+MOM by default) to get the alpha -- the paper's headline result
("four-factor alpha 0.62%, t = 2.48") that was previously stubbed out in
master_pipeline.py Section 5.

House conventions: mirrors src/ff48_industries.py (dataclass + lazy load)
and src/sic_lookup.py (urllib + cache-to-disk + descriptive User-Agent).

DATA ANATOMY (every Ken French CSV in this project):
  - descriptive comment lines, then a header row whose FIRST field is empty
    (e.g. ",Mkt-RF,SMB,HML,RMW,CMA,RF"), then data rows
    ("196307, ..." monthly / "19630701, ..." daily), then ANNUAL SUMMARY rows
    (a 4-digit year in the date field, e.g. "  2025"), then a "Copyright"
    footer. Missing values are -99.99 / -999.
  - The daily momentum file is the parser trap: a " ,, " row precedes its real
    header, and every data line ends with a trailing comma.

Everything here is in % per month for factors and returns, so an alpha of 0.62
means 0.62% per month (annualized = x12), matching the paper's units.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from src.config import FACTOR_MODEL, FF_DATA_DIR, MIN_FACTOR_OBS
from src.ff48_industries import code_to_name_map, name_to_code_map

FAMA_FRENCH_FTP = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"
_UA = {"User-Agent": "Analyst Model Research (contact: pazar.ieu2022@student.ie.edu)"}

# filename -> factor columns it contains (used as the expected-cols check)
_FF_ZIPS = {
    "ff3": "F-F_Research_Data_Factors_CSV.zip",           # Mkt-RF, SMB, HML, RF
    "ff5": "F-F_Research_Data_5_Factors_2x3_CSV.zip",     # Mkt-RF, SMB, HML, RMW, CMA, RF
    "mom": "F-F_Momentum_Factor_CSV.zip",                 # MOM
    "st_rev": "F-F_ST_Reversal_Factor_CSV.zip",           # ST_Rev
    "lt_rev": "F-F_LT_Reversal_Factor_CSV.zip",           # LT_Rev
}

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "factors")

INDUSTRY_PORTFOLIOS_ZIP = os.path.join(_CACHE_DIR, "48_Industry_Portfolios.zip")
INDUSTRY_PORTFOLIOS_CSV = os.path.join(_CACHE_DIR, "48_Industry_Portfolios.csv")

# The 48 industry codes Ken French's portfolio files are keyed by -- identical
# to the industry_code column of data/siccodes48.csv (verified 48/48 match).
INDUSTRY_CODES = set(code_to_name_map().keys())


# =============================================================================
# CSV parsing core
# =============================================================================

def _normalize_col(name: str) -> str:
    return "MOM" if name.strip().upper() == "MOM" else name.strip()


def _parse_french_csv(text: str, expected_cols: set) -> pd.DataFrame:
    """
    Parse one Ken French CSV (factor or industry-portfolio) into a DataFrame.

    Returns a DataFrame with a DatetimeIndex (month-end for monthly files,
    daily Timestamp for daily files) and one float column per parsed field;
    -99.99/-999/-99.0 sentinels become NaN.

    Header detection: the first line whose FIRST field is empty AND has >=1
    non-empty field AND every non-empty field is in expected_cols. This
    rejects comment lines, blank lines, the daily-momentum ' ,, ' empty row
    (0 non-empty fields), and any data row (first field is a date).
    """
    expected = {c.strip() for c in expected_cols}
    header = None
    out_rows: list[dict] = []

    for raw_line in text.splitlines():
        cells = [c.strip() for c in raw_line.split(",")]
        if header is None:
            if cells and cells[0] == "":
                nonempty = [_normalize_col(c) for c in cells if c]
                if nonempty and all(c in expected for c in nonempty):
                    header = [_normalize_col(c) for c in cells]
                continue  # pre-header comment/blank lines
            continue

        # Already have a header. A SECOND header-shaped line marks the start of
        # the NEXT stacked table in a multi-section Ken French file (the
        # 48-industry file has 8: VW returns, EW returns, #firms, portfolio
        # weight, etc., each repeating the same header). Only the FIRST table
        # (value-weighted monthly returns) is wanted -- stop here. Data rows
        # never look like headers (their first field is a date).
        if cells and cells[0] == "":
            nonempty = [_normalize_col(c) for c in cells if c]
            if nonempty and all(c in expected for c in nonempty):
                break
            continue

        raw_date = cells[0]
        if not raw_date.isdigit():
            continue
        if len(raw_date) == 4:
            continue  # annual summary row (e.g. '  2025')
        if len(raw_date) not in (6, 8):
            continue  # unexpected date format -- skip

        vals: dict = {}
        for i in range(1, min(len(header), len(cells))):
            col = header[i]
            if col is None or col == "":
                continue  # drop trailing empty fields (daily-momentum quirk)
            try:
                fv = float(cells[i])
            except ValueError:
                fv = np.nan
            if fv in (-99.99, -99.0, -999.0):
                fv = np.nan
            vals[col] = fv
        if vals:
            vals["_date"] = raw_date
            out_rows.append(vals)

    if not out_rows:
        raise ValueError(
            "No data rows parsed from French CSV (header detection failed -- "
            "expected columns don't match the file, or format changed)."
        )

    df = pd.DataFrame(out_rows)
    if df["_date"].str.len().eq(8).all():
        idx = pd.to_datetime(df["_date"], format="%Y%m%d")
    else:
        idx = pd.to_datetime(df["_date"].str[:4] + "-" + df["_date"].str[4:6], format="%Y-%m")
        idx = idx + pd.offsets.MonthEnd(0)
    df.index = idx
    df.index.name = "date"
    return df.drop(columns=["_date"])


def _as_frequency(df: pd.DataFrame, frequency: str = "M") -> pd.DataFrame:
    """Return monthly (default) or daily. Daily source + monthly request
    compounds returns to month-end (correct for returns)."""
    if frequency.upper().startswith("D"):
        return df
    if len(df.index) > 1 and (df.index[1] - df.index[0]).days < 20:
        # daily source -> compound to month-end
        return (1.0 + df / 100.0).resample("ME").prod() * 100.0 - 100.0
    return df


# =============================================================================
# Disk-cache helpers (mirrors src/sic_lookup.py's cache pattern)
# =============================================================================

def _read_zip_csv(zip_path: str) -> str:
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        return z.read(name).decode("utf-8", errors="replace")


def _cached_parse(key: str, source_path: str, expected_cols: set, is_zip: bool = True,
                  use_cache: bool = True) -> pd.DataFrame:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(_CACHE_DIR, f"{key}.csv")
    if use_cache and os.path.exists(cache_file):
        return pd.read_csv(cache_file, index_col="date", parse_dates=["date"])
    text = _read_zip_csv(source_path) if is_zip else open(source_path, encoding="utf-8",
                                                          errors="replace").read()
    df = _parse_french_csv(text, expected_cols)
    df.to_csv(cache_file, index_label="date")
    return df


def _resolve_zip(fname: str, download_missing: bool) -> Optional[str]:
    """Locate one F-F_*.zip in config.FF_DATA_DIR; optionally download it."""
    path = os.path.join(FF_DATA_DIR, fname)
    if os.path.exists(path):
        return path
    if not download_missing:
        return None
    url = f"{FAMA_FRENCH_FTP}/{fname}"
    print(f"[factors] downloading {url} -> {path}")
    os.makedirs(FF_DATA_DIR, exist_ok=True)
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        f.write(r.read())
    return path


# =============================================================================
# Factor data loaders
# =============================================================================

def _load_raw(key: str, cols: set, frequency: str = "M",
              download_missing: bool = False, use_cache: bool = True) -> pd.DataFrame:
    fname = _FF_ZIPS[key]
    path = _resolve_zip(fname, download_missing)
    if path is None:
        raise FileNotFoundError(
            f"Factor file not found: '{fname}' in config.FF_DATA_DIR ({FF_DATA_DIR}). "
            "Either point config.FF_DATA_DIR at your downloaded Fama-French zips "
            "or pass download_missing=True."
        )
    return _as_frequency(_cached_parse(key, path, cols, use_cache=use_cache), frequency)


def load_raw_ff3(frequency: str = "M", download_missing: bool = False, use_cache: bool = True) -> pd.DataFrame:
    """Mkt-RF, SMB, HML, RF (monthly from 192607)."""
    return _load_raw("ff3", {"Mkt-RF", "SMB", "HML", "RF"}, frequency, download_missing, use_cache)


def load_raw_ff5(frequency: str = "M", download_missing: bool = False, use_cache: bool = True) -> pd.DataFrame:
    """Mkt-RF, SMB, HML, RMW, CMA, RF (monthly from 196307)."""
    return _load_raw("ff5", {"Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"}, frequency, download_missing, use_cache)


def load_raw_momentum(frequency: str = "M", download_missing: bool = False, use_cache: bool = True) -> pd.DataFrame:
    """MOM (monthly from 192701)."""
    return _load_raw("mom", {"MOM"}, frequency, download_missing, use_cache)


def load_raw_st_reversal(frequency: str = "M", download_missing: bool = False, use_cache: bool = True) -> pd.DataFrame:
    """ST_Rev (short-term reversal, monthly)."""
    return _load_raw("st_rev", {"ST_Rev"}, frequency, download_missing, use_cache)


def load_raw_lt_reversal(frequency: str = "M", download_missing: bool = False, use_cache: bool = True) -> pd.DataFrame:
    """LT_Rev (long-term reversal, monthly)."""
    return _load_raw("lt_rev", {"LT_Rev"}, frequency, download_missing, use_cache)


@dataclass(frozen=True)
class FactorModel:
    """A cleaned, aligned monthly factor panel (DatetimeIndex, values in %/mo)."""
    factors: pd.DataFrame
    model: str
    n_obs: int


def load_factors(model: str = FACTOR_MODEL, use_cache: bool = True,
                 download_missing: bool = False) -> FactorModel:
    """
    Load the requested factor model, inner-joined on the monthly date index.
    model: 'FF3+MOM' (Carhart 4-factor), 'FF5' (pure Fama-French 5-factor),
    or 'FF5+MOM' (Fama-French 5-factor plus momentum).
    """
    model = model.upper()
    if model == "FF3+MOM":
        base = load_raw_ff3(use_cache=use_cache, download_missing=download_missing)
        cols = ["Mkt-RF", "SMB", "HML"]
        mom = load_raw_momentum(use_cache=use_cache, download_missing=download_missing)
        joined = base.join(mom, how="inner")[cols + ["MOM", "RF"]].dropna(subset=cols + ["MOM"])
    elif model == "FF5":
        base = load_raw_ff5(use_cache=use_cache, download_missing=download_missing)
        cols = ["Mkt-RF", "SMB", "HML", "RMW", "CMA"]
        joined = base[cols + ["RF"]].dropna(subset=cols)
    elif model == "FF5+MOM":
        base = load_raw_ff5(use_cache=use_cache, download_missing=download_missing)
        cols = ["Mkt-RF", "SMB", "HML", "RMW", "CMA"]
        mom = load_raw_momentum(use_cache=use_cache, download_missing=download_missing)
        joined = base.join(mom, how="inner")[cols + ["MOM", "RF"]].dropna(subset=cols + ["MOM"])
    else:
        raise ValueError(f"Unknown factor model {model!r} -- use 'FF3+MOM', 'FF5', or 'FF5+MOM'")
    return FactorModel(factors=joined, model=model, n_obs=len(joined))


# =============================================================================
# 48 Industry Portfolios (monthly value-weighted industry returns)
# =============================================================================

def download_industry_portfolios() -> str:
    """Download + unzip Ken French's 48 Industry Portfolios (monthly VW) into
    data/factors/ if not already cached. 0 FactSet calls. Returns the CSV path."""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    if not os.path.exists(INDUSTRY_PORTFOLIOS_CSV):
        if not os.path.exists(INDUSTRY_PORTFOLIOS_ZIP):
            url = f"{FAMA_FRENCH_FTP}/48_Industry_Portfolios_CSV.zip"
            print(f"[factors] downloading {url}")
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=60) as r, open(INDUSTRY_PORTFOLIOS_ZIP, "wb") as f:
                f.write(r.read())
        with zipfile.ZipFile(INDUSTRY_PORTFOLIOS_ZIP) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            with open(INDUSTRY_PORTFOLIOS_CSV, "wb") as out:
                out.write(z.read(name))
    return INDUSTRY_PORTFOLIOS_CSV


def load_industry_portfolio_returns(frequency: str = "M", use_cache: bool = True) -> pd.DataFrame:
    """
    Monthly value-weighted returns for the 48 Fama-French industries, one
    column per industry CODE, DatetimeIndex (month-end), values in %/mo.
    Downloads on first use (cached to data/factors/), 0 FactSet calls.
    """
    try:
        csv_path = download_industry_portfolios()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[factors] WARNING: couldn't fetch 48-industry portfolios ({e}) -- "
              f"strategy backtest needs network on first use. Re-run once online, "
              f"or pre-place data/factors/48_Industry_Portfolios.csv.")
        return pd.DataFrame()
    df = _cached_parse("industry_portfolios", csv_path, INDUSTRY_CODES, is_zip=False, use_cache=use_cache)
    if df.shape[1] != len(INDUSTRY_CODES):
        print(f"[factors] WARNING: 48-industry file parsed to {df.shape[1]} columns "
              f"(expected {len(INDUSTRY_CODES)}) -- alignment may be off.")
    return _as_frequency(df, frequency)


# =============================================================================
# Long-Short Strategy Returns Construction
# =============================================================================

def build_long_short_returns(
    industry_sentiment: pd.DataFrame,
    industry_portfolio_returns: pd.DataFrame,
    n_per_leg: int = 5,
    weight_scheme: str = "equal",
) -> pd.Series:
    """
    Build monthly Long-Short portfolio returns from industry-level sentiment
    and Ken French's 48 industry value-weighted portfolio returns.

    Paper methodology (Eq. 6, p.31, Section 4.5):
    - Monthly sort of 48 industries by Industry-Level Analyst Sentiment
    - Long = K lowest-sentiment industries, Short = K highest
    - Value-weighted within each leg (using French's VW industry returns)
    - Rebalanced monthly

    Parameters:
    - industry_sentiment: output of industry_level() -- columns [industry, quarter, industry_sentiment, ...]
      Industry names are the full names (e.g., "Computers"), which we map to French codes.
    - industry_portfolio_returns: output of load_industry_portfolio_returns() -- columns are industry CODES
      (e.g., "Comps"), DatetimeIndex (month-end), values in %/mo.
    - n_per_leg: number of industries per leg (paper uses K=5)
    - weight_scheme: "equal" (paper default -- French's VW industry returns are already
      value-weighted WITHIN industry; equal-weighting across industries is the
      paper's interim implementation since we don't have cross-industry market caps
      from the single-ticker universe yet)

    Returns a pd.Series with DatetimeIndex (month-end), name="ls_excess_return",
    values in %/mo (already excess returns since French's industry returns are
    excess returns over RF). This is the y-variable for the factor regression.
    """
    if industry_sentiment.empty or industry_portfolio_returns.empty:
        return pd.Series(dtype=float, name="ls_excess_return")

    # Map industry NAME to industry CODE for joining with French portfolios
    name_to_code = name_to_code_map()
    industry_sentiment = industry_sentiment.copy()
    industry_sentiment["industry_code"] = industry_sentiment["industry"].map(name_to_code)

    # Drop UNMAPPED and any industries without a code
    industry_sentiment = industry_sentiment.dropna(subset=["industry_code", "industry_sentiment"])

    if industry_sentiment.empty:
        return pd.Series(dtype=float, name="ls_excess_return")

    # Convert quarter (e.g., "2015Q1") to the NEXT month-end return period.
    # This deliberately lags the quarterly signal by one month to avoid
    # same-period look-ahead. Exact event-date timing can replace this later.
    def quarter_to_return_month_end(q_str: str) -> pd.Timestamp:
        """Map a quarterly signal to the NEXT month-end return period.

        This conservative one-month lag prevents a quarter-end signal from
        earning a return from the same month. Exact event-date timing can
        replace this convention once the multi-company strategy panel carries
        signal-availability dates.
        """
        year = int(q_str[:4])
        quarter = int(q_str[5])
        signal_month = quarter * 3
        signal_date = pd.Timestamp(year, signal_month, 1) + pd.offsets.MonthEnd(0)
        return signal_date + pd.offsets.MonthEnd(1)

    industry_sentiment["date"] = industry_sentiment["quarter"].apply(quarter_to_return_month_end)

    # Align: for each month, pick the K lowest/highest sentiment industries
    # that have returns available that month
    monthly_returns = []

    # Get the union of dates from both data sources
    sentiment_dates = industry_sentiment["date"].unique()
    portfolio_dates = industry_portfolio_returns.index

    # We iterate through months where we have BOTH sentiment signal AND returns
    common_dates = sorted(set(sentiment_dates) & set(portfolio_dates))

    for dt in common_dates:
        # Get sentiment for this month (may have multiple quarters mapping to same month)
        month_sentiment = industry_sentiment[industry_sentiment["date"] == dt].dropna(
            subset=["industry_code", "industry_sentiment"]
        )
        if len(month_sentiment) < 2 * n_per_leg:
            continue

        # Sort by sentiment: lowest -> Long, highest -> Short
        month_sentiment = month_sentiment.sort_values("industry_sentiment")

        long_codes = month_sentiment.head(n_per_leg)["industry_code"].tolist()
        short_codes = month_sentiment.tail(n_per_leg)["industry_code"].tolist()

        # Get returns for these industries this month
        if dt not in industry_portfolio_returns.index:
            continue

        month_rets = industry_portfolio_returns.loc[dt]

        # Filter to codes that exist in the portfolio returns
        long_codes_avail = [c for c in long_codes if c in month_rets.index]
        short_codes_avail = [c for c in short_codes if c in month_rets.index]

        if not long_codes_avail or not short_codes_avail:
            continue

        # Weighting: equal-weight across industries (paper's default)
        # French's industry returns are already VW within industry
        long_ret = month_rets[long_codes_avail].mean()
        short_ret = month_rets[short_codes_avail].mean()

        # Long-Short excess return (already excess since French returns are excess)
        ls_ret = long_ret - short_ret
        monthly_returns.append({"date": dt, "ls_excess_return": ls_ret})

    if not monthly_returns:
        return pd.Series(dtype=float, name="ls_excess_return")

    result = pd.DataFrame(monthly_returns).set_index("date")["ls_excess_return"]
    result.index = pd.DatetimeIndex(result.index)
    result.name = "ls_excess_return"
    return result.sort_index()


# =============================================================================
# Factor backtest orchestration (Section 7)
# =============================================================================

@dataclass(frozen=True)
class FactorBacktestResult:
    """
    Complete result of the factor-model risk-adjustment backtest.
    """
    ls_returns: pd.Series              # Monthly Long-Short excess returns
    factor_model: FactorModel          # The factor panel used
    regression: FactorRegressionResult # OLS regression result
    model_name: str                    # "FF3+MOM" or "FF5+MOM"
    n_months: int                      # Number of months in the backtest
    start_date: pd.Timestamp           # First month
    end_date: pd.Timestamp             # Last month


def run_factor_backtest(
    industry_sentiment: pd.DataFrame,
    factor_model_name: str = FACTOR_MODEL,
    n_per_leg: int = 5,
    weight_scheme: str = "equal",
    newey_west_lags: int = 0,
    use_cache: bool = True,
    download_missing: bool = False,
) -> Optional[FactorBacktestResult]:
    """
    Orchestrate the complete factor-model risk-adjustment backtest for the
    industry Long-Short strategy (paper Eq. 6 / Section 4.5, p.31).

    Steps:
    1. Load the requested factor model (FF3+MOM or FF5+MOM)
    2. Load 48-industry portfolio returns (monthly VW from Ken French)
    3. Build Long-Short strategy returns from industry sentiment
    4. Regress L-S excess returns on factor model (OLS, optionally Newey-West)
    5. Return all results in a structured dataclass

    The L-S returns are already excess returns (French's industry portfolios
    are excess returns over RF), so we regress them directly on the factor
    panel WITHOUT subtracting RF again.

    Returns None if there isn't enough overlapping data.
    """
    # 1. Load factors
    factor_model_obj = load_factors(
        model=factor_model_name,
        use_cache=use_cache,
        download_missing=download_missing,
    )
    factor_panel = factor_model_obj.factors

    # 2. Load industry portfolio returns
    industry_portfolio_rets = load_industry_portfolio_returns(frequency="M", use_cache=use_cache)
    if industry_portfolio_rets.empty:
        print("[factor_backtest] WARNING: No industry portfolio returns available")
        return None

    # 3. Build Long-Short returns
    ls_returns = build_long_short_returns(
        industry_sentiment=industry_sentiment,
        industry_portfolio_returns=industry_portfolio_rets,
        n_per_leg=n_per_leg,
        weight_scheme=weight_scheme,
    )

    if ls_returns.empty:
        print("[factor_backtest] WARNING: No Long-Short returns could be constructed")
        return None

    # 4. Regress L-S excess returns on factors
    # The RF column is NOT a regressor; y is already excess returns
    regressors = factor_panel.drop(columns=["RF"], errors="ignore")
    regression = regress_on_factors(ls_returns, regressors, newey_west_lags=newey_west_lags)

    if regression is None:
        print("[factor_backtest] WARNING: Factor regression failed (insufficient overlap)")
        return None

    # 5. Package result
    return FactorBacktestResult(
        ls_returns=ls_returns,
        factor_model=factor_model_obj,
        regression=regression,
        model_name=factor_model_name,
        n_months=len(ls_returns),
        start_date=ls_returns.index.min(),
        end_date=ls_returns.index.max(),
    )


# =============================================================================
# Factor regression (pure numpy -- no statsmodels dependency)
# =============================================================================

@dataclass
class FactorRegressionResult:
    alpha: float               # intercept, % per month
    alpha_tstat: float
    alpha_annualized: float    # alpha * 12
    loadings: dict             # {factor: beta}
    loadings_tstats: dict      # {factor: t-stat}
    r_squared: float
    n_obs: int
    mean_monthly_return: float
    std_monthly_return: float
    sharpe_annualized: float   # sqrt(12) * mean / std

    def summary_dict(self) -> list:
        rows = [
            ("alpha_monthly_pct", self.alpha),
            ("alpha_tstat", self.alpha_tstat),
            ("alpha_annualized_pct", self.alpha_annualized),
            ("mean_monthly_return_pct", self.mean_monthly_return),
            ("std_monthly_return_pct", self.std_monthly_return),
            ("sharpe_annualized", self.sharpe_annualized),
            ("r_squared", self.r_squared),
            ("n_obs", self.n_obs),
        ]
        for f in self.loadings:
            rows.append((f"loading_{f}", self.loadings[f]))
            rows.append((f"loading_tstat_{f}", self.loadings_tstats.get(f)))
        return rows


def regress_on_factors(portfolio_excess_return: pd.Series, factor_panel: pd.DataFrame,
                       newey_west_lags: int = 0) -> Optional[FactorRegressionResult]:
    """
    Carhart 4-factor OLS: y = alpha + b*(Mkt-RF) + s*SMB + h*HML + m*MOM + e,
    where y = portfolio EXCESS return (%/mo, RF already subtracted by caller).
    Regressors come from factor_panel; the RF column is NOT a regressor.

    Inner-joins on the DatetimeIndex, drops rows with NaN in y or any
    regressor. Returns None when there aren't enough overlapping obs.
    newey_west_lags > 0 enables a Bartlett HAC sandwich (for autocorrelated
    L-S returns); default 0 = plain OLS.
    """
    if portfolio_excess_return is None or len(portfolio_excess_return) == 0:
        return None
    df = pd.concat([portfolio_excess_return.rename("y"), factor_panel], axis=1, join="inner").dropna()
    factor_cols = [c for c in ("Mkt-RF", "SMB", "HML", "MOM", "RMW", "CMA") if c in factor_panel.columns]
    if not factor_cols:
        return None
    y = df["y"].values
    X = df[factor_cols].values
    n, k = len(y), len(factor_cols)
    if n <= k + 1:
        return None
    Xd = np.column_stack([np.ones(n), X])
    beta, *_ = np.linalg.lstsq(Xd, y, rcond=None)
    resid = y - Xd @ beta
    dof = n - (k + 1)
    sigma2 = resid @ resid / dof
    cov = sigma2 * np.linalg.pinv(Xd.T @ Xd)
    if newey_west_lags > 0:
        Xep = Xd * resid[:, None]
        S = Xep.T @ Xep
        for lag in range(1, newey_west_lags + 1):
            w = 1 - lag / (newey_west_lags + 1)
            S += w * (Xep[lag:].T @ Xep[:-lag] + Xep[:-lag].T @ Xep[lag:])
        cov = np.linalg.pinv(Xd.T @ Xd) @ S @ np.linalg.pinv(Xd.T @ Xd)
    se = np.sqrt(np.diag(cov))
    tstats = beta / se

    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r_squared = 1.0 - float(resid @ resid) / ss_tot if ss_tot > 0 else float("nan")
    mean_r = float(np.mean(y))
    std_r = float(np.std(y, ddof=1)) if n > 1 else float("nan")

    return FactorRegressionResult(
        alpha=float(beta[0]),
        alpha_tstat=float(tstats[0]),
        alpha_annualized=float(beta[0]) * 12.0,
        loadings={c: float(b) for c, b in zip(factor_cols, beta[1:])},
        loadings_tstats={c: float(t) for c, t in zip(factor_cols, tstats[1:])},
        r_squared=r_squared,
        n_obs=n,
        mean_monthly_return=mean_r,
        std_monthly_return=std_r,
        sharpe_annualized=(float(np.sqrt(12.0)) * mean_r / std_r) if std_r and std_r == std_r else float("nan"),
    )


# =============================================================================
# Synthetic returns + selfcheck (the correctness proof)
# =============================================================================

def make_synthetic_strategy_returns(factor_panel: pd.DataFrame, alpha: float = 0.62,
                                    loadings: Optional[dict] = None, noise_scale: float = 1.0,
                                    seed: int = 0) -> pd.Series:
    """
    r[t] = alpha + X_t @ loadings + N(0, noise_scale^2) over the panel index.
    Default loadings: Mkt-RF 0.0, SMB 0.1, HML -0.3, MOM -0.2. Returns a
    DatetimeIndex Series of monthly EXCESS returns (%/mo).
    """
    rng = np.random.default_rng(seed)
    factor_cols = [c for c in ("Mkt-RF", "SMB", "HML", "MOM", "RMW", "CMA") if c in factor_panel.columns]
    defaults = {"SMB": 0.1, "HML": -0.3, "MOM": -0.2}
    if loadings is None:
        loadings = {c: defaults.get(c, 0.0) for c in factor_cols}
    X = factor_panel[factor_cols].values
    true_beta = np.array([loadings.get(c, 0.0) for c in factor_cols])
    noise = rng.normal(0.0, noise_scale, len(X))
    r = alpha + X @ true_beta + noise
    return pd.Series(r, index=factor_panel.index, name="excess_return")


def _synthetic_factor_panel(n_months: int = 120, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2015-01-31", periods=n_months, freq="ME")
    return pd.DataFrame(
        {
            "Mkt-RF": rng.normal(0.5, 4.0, n_months),
            "SMB": rng.normal(0.2, 2.5, n_months),
            "HML": rng.normal(0.2, 3.0, n_months),
            "MOM": rng.normal(0.3, 4.0, n_months),
            "RF": np.full(n_months, 0.15),
        },
        index=idx,
    )


def selfcheck(tol_alpha: float = 0.05, tol_loading: float = 0.05) -> bool:
    """
    THE correctness proof: inject a KNOWN alpha (0.62%/mo, the paper's) into
    synthetic strategy returns built on real factor history, run the OLS
    regression, and confirm it recovers the injected alpha + loadings. This is
    what answers "is the alpha real or fabricated?": if the machinery provably
    recovers a planted alpha, then any nonzero alpha a future real sentiment
    run produces is attributable to the sentiment data, not a regression bug.
    """
    print("=== factors selfcheck: recover an injected alpha from synthetic returns ===")
    try:
        panel = load_factors(FACTOR_MODEL).factors
        print(f"loaded real factor panel: {panel.shape[0]} months, "
              f"{panel.index.min().date()} .. {panel.index.max().date()}")
    except Exception as e:  # noqa: BLE001 -- data dir missing is fine for the selfcheck
        print(f"real factor panel unavailable ({e}) -- using synthetic factor panel")
        panel = _synthetic_factor_panel()

    true_alpha = 0.62
    true_loadings = {"Mkt-RF": 0.0, "SMB": 0.1, "HML": -0.3, "MOM": -0.2}
    r = make_synthetic_strategy_returns(panel, alpha=true_alpha, loadings=true_loadings,
                                        noise_scale=1.0, seed=0)
    res = regress_on_factors(r, panel)
    if res is None:
        print("FAIL: regression returned None (not enough observations)")
        return False

    ok = abs(res.alpha - true_alpha) <= tol_alpha
    print(f"alpha: recovered {res.alpha:+.4f}%/mo, true {true_alpha:+.2f} (t={res.alpha_tstat:+.2f}) "
          f"{'PASS' if abs(res.alpha - true_alpha) <= tol_alpha else 'FAIL'}")
    for col, true_val in true_loadings.items():
        got = res.loadings.get(col, float("nan"))
        good = abs(got - true_val) <= tol_loading
        ok = ok and good
        print(f"  loading {col:6s}: recovered {got:+.4f}, true {true_val:+.2f} {'PASS' if good else 'FAIL'}")
    print(f"r_squared={res.r_squared:.3f}, n_obs={res.n_obs}, sharpe_annualized={res.sharpe_annualized:.2f}")
    print(f"OVERALL: {'PASS' if ok else 'FAIL'}")
    return ok


# =============================================================================
# Per-analyst factor-alpha on forecast-error residuals (Factor-Adjusted Scoring)
# =============================================================================

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


if __name__ == "__main__":
    sys.exit(0 if selfcheck() else 1)
