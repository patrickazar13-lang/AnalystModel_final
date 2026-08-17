"""
Production data-access layer for the LIVE FactSet pull.

Every probe in src/estimates_via_formula.py (rounds 1-6) was exploratory --
this module is where the CONFIRMED-WORKING formula shapes get turned into
clean, typed functions that master_pipeline.py's fetch_live_ticker_data()
calls directly. Nothing in here is a guess; every formula string used below
was verified against real AAPL-US data during the probe rounds.

COST MODEL (read this before changing anything): FactSet bills API "points"
per matched IDENTIFIER, not per formula -- a call with ids=["AAPL-US"] costs
exactly 1 point no matter how many formulas are bundled into it, as long as
every formula targets that SAME identifier. Every function below is written
to exploit that: each one makes exactly ONE HTTP call and is annotated with
its point cost. Total cost for one ticker's full history pull with the
default settings (config.LIVE_N_QUARTERS = 12, i.e. 3 years back to 2023 --
raised from an initial 6 once "2 years might not be enough" for a real
per-analyst history):

    1   (quarterly EPS history, all quarters bundled in 1 call)
  + 12  (price + market cap, ONE call per quarter -- see note in
         get_price_and_mktcap_on_date for why these are NOT bundled)
  + 12  (analyst EPS snapshot, ONE call per quarter -- same reason)
  ----
  = 25 points for one ticker (was 13 at the original 6-quarter setting)

WHY PRICE/MKTCAP AND ANALYST SNAPSHOTS AREN'T BUNDLED ACROSS QUARTERS:
the quarterly-EPS bundle (get_quarterly_eps_history) is safe to bundle
because round 6 PROVED the row-alignment behavior for relative QTR_R
offsets (each offset's value lands in the row matching its own resolved
quarter-end date -- see that function's docstring for the exact evidence).
We have NOT tested whether the same clean alignment holds when you bundle
several *absolute*-date formulas (e.g. P_PRICE('20250414',...) and
P_PRICE('20250115',...)) in one call, or several SNAP-dated
FE_BROKER_ESTIMATE calls in one call. Guessing wrong here would silently
mix up which analyst's estimate or which day's price belongs to which
quarter -- a correctness bug, not just an inconvenience. Given the call
budget has plenty of headroom (config.SESSION_CALL_BUDGET), we spend a few
extra points per ticker to stay unambiguously correct. If you want to
widen this to a real multi-ticker universe later, run ONE probe first
(bundle 2 known dates for one ticker, inspect the raw response) before
switching these to bundled calls -- don't assume it works the same way.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pandas as pd

from src.factset_client import fetch_time_series

try:
    import pandas_market_calendars as mcal

    _NYSE = mcal.get_calendar("NYSE")
except ImportError:  # pragma: no cover - degrade gracefully, see trading_days_before()
    _NYSE = None


# =============================================================================
# Trading-day arithmetic (pure Python/pandas -- no API cost)
# =============================================================================

def trading_days_before(date_str: str, n: int = 10) -> str:
    """
    Returns the calendar date (YYYYMMDD) that is exactly `n` NYSE trading
    days before date_str (YYYYMMDD). This is what the paper's Eq. 1 (p.11)
    means by "P_{j,d-10}" -- the price 10 TRADING days (not calendar days)
    before the earnings announcement.

    Uses the real NYSE holiday/weekend calendar via pandas_market_calendars
    when it's installed (added to requirements.txt). Falls back to a naive
    Mon-Fri-only approximation if the package is missing, and prints a loud
    warning when it does -- that fallback silently ignores market holidays
    (Thanksgiving, Christmas, etc.), so a date near a holiday could be off
    by one session. Run `pip install pandas-market-calendars` to remove
    this caveat entirely.
    """
    d = dt.datetime.strptime(date_str, "%Y%m%d").date()

    if _NYSE is not None:
        schedule = _NYSE.schedule(
            start_date=d - dt.timedelta(days=int(n * 2.5) + 15),
            end_date=d - dt.timedelta(days=1),
        )
        sessions = schedule.index
        if len(sessions) < n:
            raise ValueError(
                f"Not enough NYSE sessions before {date_str} to step back {n} trading days "
                f"(found {len(sessions)}). Widen the lookback window in trading_days_before()."
            )
        return sessions[-n].strftime("%Y%m%d")

    print(
        "[factset_data] WARNING: pandas_market_calendars not installed -- using a "
        "naive Mon-Fri approximation (ignores exchange holidays). "
        "pip install pandas-market-calendars for exact NYSE trading-day math."
    )
    cur = d
    steps = 0
    while steps < n:
        cur -= dt.timedelta(days=1)
        if cur.weekday() < 5:  # Mon=0 .. Fri=4
            steps += 1
    return cur.strftime("%Y%m%d")


# =============================================================================
# 1. Quarterly actual EPS + report dates
#    Formulas confirmed in probe round 6 (probe_qtr_mktcap.py):
#      FF_EPS(QTR_R,-{k}Q)          -- actual reported EPS, k quarters back
#      FF_EPS_RPT_DATE(QTR_R,-{k}Q) -- that quarter's report date
#
#    EVIDENCE this is safe to bundle across all k in ONE call: round 6's
#    real response had rows keyed by resolved quarter-end date, e.g.
#      FF_EPS(QTR_R,-1Q)          -> [2.8424, None,   None,   None]
#      FF_EPS(QTR_R,-2Q)          -> [None,   1.8479, None,   None]
#      FF_EPS_RPT_DATE(QTR_R,-1Q) -> ['20260129', None, None, None]
#      date                       -> ['2025-12-31','2025-09-30','2025-06-30','2025-03-31']
#    i.e. each offset's non-None value lands in the row whose "date" matches
#    that offset's own resolved quarter -- rows never collide. Requesting
#    -1Q..-{k}Q AND the matching _RPT_DATE offsets together is the same
#    pattern, just more of it, so it stays 1 API point.
# =============================================================================

def get_quarterly_eps_history(ticker: str, n_quarters: int = 12) -> pd.DataFrame:
    """
    Cost: 1 API point (single ticker, all n_quarters bundled).

    Returns a DataFrame with one row per quarter, columns:
      quarter_end   -- e.g. '2025-12-31' (from FactSet's own "date" field)
      actual_eps    -- reported EPS for that quarter
      report_date   -- earnings announcement date, YYYYMMDD string
    sorted most-recent-quarter first.
    """
    formulas = []
    for k in range(1, n_quarters + 1):
        formulas.append(f"FF_EPS(QTR_R,-{k}Q)")
        formulas.append(f"FF_EPS_RPT_DATE(QTR_R,-{k}Q)")

    raw = fetch_time_series(ids=[ticker], formulas=formulas)
    rows = raw.get("data", [])

    out = []
    for row in rows:
        quarter_end = row.get("date")
        eps = next(
            (v for f, v in row.items() if f.startswith("FF_EPS(") and v is not None),
            None,
        )
        rpt_date = next(
            (v for f, v in row.items() if f.startswith("FF_EPS_RPT_DATE(") and v is not None),
            None,
        )
        if quarter_end is None or (eps is None and rpt_date is None):
            continue
        quarter_end_str = str(quarter_end) if quarter_end is not None else None
        quarter_label = None
        year = None
        if quarter_end_str and len(quarter_end_str) >= 7:
            year = int(quarter_end_str[:4])
            month = int(quarter_end_str[5:7])
            quarter_label = f"{year}Q{((month - 1) // 3) + 1}"
        out.append({
            "ticker": ticker,
            "quarter_end": quarter_end,
            "quarter": quarter_label,
            "year": year,
            "actual_eps": eps,
            "report_date": rpt_date,
        })

    df = pd.DataFrame(out)
    if not df.empty:
        df = df.sort_values("quarter_end", ascending=False).reset_index(drop=True)
    return df


# =============================================================================
# 2. Price + market cap on ONE specific calendar date
#    Confirmed formulas:
#      P_PRICE('{date}',,,USD)   -- theone-event-study, proven working
#      P_MKT_VAL(0)              -- probe round 6 confirmed the FORMULA
#                                    NAME is valid and returns a real number,
#                                    but round 6 only tried the "0" (current)
#                                    offset, which returned the SAME value
#                                    replicated across every row -- i.e. NOT
#                                    yet proven to accept a literal past date
#                                    the way P_PRICE does.
#      FF_COM_SHS_OUT('{date}')  -- shares outstanding; used as a fallback so
#                                    market cap is always computable even if
#                                    P_MKT_VAL's date argument turns out not
#                                    to work as hoped (market_cap = price *
#                                    shares_outstanding is the textbook
#                                    definition Eq. 4/5 need anyway).
# =============================================================================

def get_price_and_mktcap_on_date(ticker: str, date_yyyymmdd: str) -> dict:
    """
    Cost: 1 API point (single ticker, 3 formulas bundled for the SAME date --
    no cross-date ambiguity since every formula in this call targets the
    identical date).

    Returns {"date": ..., "price": float|None, "mkt_val_direct": float|None,
             "shares_out": float|None, "market_cap": float|None}
    where market_cap prefers P_MKT_VAL if it returned a usable point-in-time
    number, else falls back to price * shares_out.
    """
    d = date_yyyymmdd
    formulas = [
        f"P_PRICE('{d}',,,USD)",
        f"P_MKT_VAL('{d}',,,USD)",
        f"FF_COM_SHS_OUT('{d}')",
    ]
    raw = fetch_time_series(ids=[ticker], formulas=formulas)
    rows = raw.get("data", [])
    row = rows[0] if rows else {}

    price = next((v for f, v in row.items() if f.startswith("P_PRICE(") and v is not None), None)
    mkt_val_direct = next(
        (v for f, v in row.items() if f.startswith("P_MKT_VAL(") and v is not None), None
    )
    shares_out = next(
        (v for f, v in row.items() if f.startswith("FF_COM_SHS_OUT(") and v is not None), None
    )

    # FactSet can return an FQL error string for a historical shares-outstanding
    # request even when the same call returns a valid price and point-in-time
    # market value. Keep the raw pull clean: treat that error as missing data
    # rather than writing the API error text into the CSV.
    if isinstance(shares_out, str) and shares_out.startswith("%FQL-"):
        shares_out = None

    if mkt_val_direct is not None:
        market_cap = mkt_val_direct
    elif price is not None and shares_out is not None:
        market_cap = price * shares_out
    else:
        market_cap = None

    market_cap_source = "P_MKT_VAL" if mkt_val_direct is not None else ("price*shares_out" if price is not None and shares_out is not None else None)
    return {
        "ticker": ticker,
        "date": d,
        "price_date": d,
        "price": price,
        "mkt_val_direct": mkt_val_direct,
        "shares_out": shares_out,
        "market_cap": market_cap,
        "market_cap_source": market_cap_source,
    }


# =============================================================================
# 3. Per-analyst EPS estimate snapshot on ONE date
#    Confirmed formulas (probe round 4, probe_eps_formulas_v4.py):
#      FE_BROKER_ESTIMATE(SNAP,EPS,EST_VALUE,,,'{date}')
#      FE_BROKER_ESTIMATE(SNAP,EPS,BKR_NAME,,,'{date}')
#      FE_BROKER_ESTIMATE(SNAP,EPS,AN_NAME,,,'{date}')
#      FE_BROKER_ESTIMATE_DATE(SNAP,EPS,MODDATEN,,,'YYYYMMDD','{date}')
#    all returned real, ROW-ALIGNED data for ~43 analysts in one call.
# =============================================================================

def get_analyst_eps_snapshot(ticker: str, date_yyyymmdd: str) -> pd.DataFrame:
    """
    Cost: 1 API point (single ticker, 5 formulas bundled for the SAME
    snapshot date).

    Returns a DataFrame with one row per contributing analyst:
      analyst, broker, broker_code, est_value, revision_date, snapshot_date

    BKR_CODE (confirmed working -- Patrick's candidate formula, verified
    against real AAPL-US data): a stable numeric broker identifier, useful
    for filtering/grouping by brokerage without the fuzzy-matching issues
    free-text BKR_NAME can have (spelling/punctuation drift across
    snapshots). Notably, BKR_CODE is populated even on rows where BKR_NAME
    and AN_NAME both show 'Restricted' -- that broker opted out of
    identity redistribution, but its CODE still comes through.

    Rows where the analyst name is the literal string 'Restricted' are
    still DROPPED here (as before) -- we now know the broker's identity on
    those rows via broker_code, but we still can't attribute a reliability
    score to an anonymous INDIVIDUAL analyst, and multiple different
    restricted analysts at the same broker would incorrectly collapse into
    one identity if we used broker_code as a stand-in for analyst identity.
    If you want those rows included as a "this broker's unnamed analysts"
    bucket later, that's a deliberate design change to make explicitly, not
    a side effect of adding this field.
    """
    d = date_yyyymmdd
    formulas = [
        f"FE_BROKER_ESTIMATE(SNAP,EPS,EST_VALUE,,,'{d}')",
        f"FE_BROKER_ESTIMATE(SNAP,EPS,BKR_NAME,,,'{d}')",
        f"FE_BROKER_ESTIMATE(SNAP,EPS,BKR_CODE,,,'{d}')",
        f"FE_BROKER_ESTIMATE(SNAP,EPS,AN_NAME,,,'{d}')",
        f"FE_BROKER_ESTIMATE_DATE(SNAP,EPS,MODDATEN,,,'YYYYMMDD','{d}')",
    ]
    raw = fetch_time_series(ids=[ticker], formulas=formulas)
    rows = raw.get("data", [])

    est_col = next((f for f in (rows[0].keys() if rows else []) if "EST_VALUE" in f), None)
    bkr_col = next((f for f in (rows[0].keys() if rows else []) if "BKR_NAME" in f), None)
    code_col = next((f for f in (rows[0].keys() if rows else []) if "BKR_CODE" in f), None)
    an_col = next((f for f in (rows[0].keys() if rows else []) if "AN_NAME" in f), None)
    mod_col = next((f for f in (rows[0].keys() if rows else []) if "MODDATEN" in f), None)

    out = []
    for row in rows:
        analyst = row.get(an_col) if an_col else None
        if not analyst or analyst == "Restricted":
            continue
        out.append(
            {
                "ticker": ticker,
                "analyst": analyst,
                "broker": row.get(bkr_col) if bkr_col else None,
                "broker_code": row.get(code_col) if code_col else None,
                "est_value": row.get(est_col) if est_col else None,
                "estimate_value": row.get(est_col) if est_col else None,
                "revision_date": row.get(mod_col) if mod_col else None,
                "snapshot_date": d,
                "estimate_snapshot_date": d,
            }
        )

    return pd.DataFrame(out)
