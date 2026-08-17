"""
backfill_sic.py
================
One-off fixer: your KLAC-US live pull ran BEFORE config.TICKER_SIC_OVERRIDES
had a "KLAC-US" entry, so the raw CSV on disk has sic_code=None baked in for
every row and industry aggregation shows "UNMAPPED".

The SIC code doesn't need a fresh FactSet call to fix -- it's just a static
lookup value. This script re-reads the raw data you already pulled, patches
in the correct sic_code from config.TICKER_SIC_OVERRIDES, and re-runs the
(entirely local, 0-API-call) scoring/aggregation steps -- then rewrites the
same output CSVs export_to_excel.py reads.

Cost: 0 FactSet API calls. Doesn't touch CallBudget at all.

Usage:
    python backfill_sic.py --ticker KLAC-US
"""

from __future__ import annotations

import argparse
import os

import pandas as pd

from master_pipeline import run_pipeline
from src.config import TICKER_SIC_OVERRIDES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True, help="e.g. KLAC-US")
    args = parser.parse_args()

    ticker = args.ticker
    safe_ticker = ticker.replace("-", "_")
    raw_path = f"outputs/live_{safe_ticker}_raw_forecast_errors.csv"

    if not os.path.exists(raw_path):
        raise SystemExit(
            f"Can't find {raw_path} -- this script only patches data from a "
            f"live pull that already ran. Run "
            f"`python master_pipeline.py --live --ticker {ticker}` first."
        )

    sic_code = TICKER_SIC_OVERRIDES.get(ticker)
    if sic_code is None:
        raise SystemExit(
            f"No SIC override on file for {ticker} in src/config.py yet -- "
            f"add one to TICKER_SIC_OVERRIDES before running this."
        )

    raw_fe = pd.read_csv(raw_path)
    n_missing = raw_fe["sic_code"].isna().sum()
    print(f"Loaded {raw_path}: {len(raw_fe)} rows, {n_missing} with missing sic_code.")

    raw_fe["sic_code"] = sic_code
    print(f"Patched sic_code -> {sic_code} for all {len(raw_fe)} rows.")

    raw_fe.to_csv(raw_path, index=False)

    results = run_pipeline(raw_fe)

    results["consensus"].to_csv(f"outputs/live_{safe_ticker}_consensus.csv", index=False)
    results["industry"].to_csv(f"outputs/live_{safe_ticker}_industry_sentiment.csv", index=False)
    results["analyst_scores"].to_csv(f"outputs/live_{safe_ticker}_analyst_scores.csv", index=False)

    print(f"\nRewrote outputs/live_{safe_ticker}_consensus.csv, "
          f"_industry_sentiment.csv, _analyst_scores.csv with the corrected "
          f"industry mapping (0 API calls spent).")
    print("Now re-run: python export_to_excel.py --ticker " + ticker)


if __name__ == "__main__":
    main()
