"""
Constituents / weights for an index or ETF, via the Formula API -- the
Python equivalent of the FactSet training example:

    Universe:  (FG_CONSTITUENTS("EXSA-DE",0,CLOSE))=1
    Formula:   ETP_CONST_WEIGHT("EXSA-DE",0)

Two entry points:
  - get_constituent_weight_single(ticker, etf_ticker): cheap, 1 API point,
    for testing the formula shape against ONE known member of the ETF
    before spending points on the full universe.
  - get_universe_weights(etf_ticker): the real thing -- pulls every
    constituent + its weight in ONE call, priced at ~N API points where N
    is the constituent count (e.g. ~600 for Stoxx 600). Don't call this
    until the single-ticker version is confirmed working.
"""

from src.factset_client import fetch_time_series
from src.config import EXAMPLE_ETF_TICKER


def get_constituent_weight_single(ticker: str, etf_ticker: str = EXAMPLE_ETF_TICKER) -> dict:
    """
    Confirms ETP_CONST_WEIGHT works and returns a sane value for a single,
    already-known member of the ETF -- 1 API point, not a universe call.
    """
    formula = f'ETP_CONST_WEIGHT("{etf_ticker}",0)'
    return fetch_time_series(ids=[ticker], formulas=[formula])


def get_universe_weights(etf_ticker: str = EXAMPLE_ETF_TICKER) -> dict:
    """
    Full universe pull: every current constituent of etf_ticker plus its
    weight. API points billed = number of constituents matched (~600 for
    Stoxx 600) -- confirm the formula shape with
    get_constituent_weight_single() first.
    """
    universe = f"(FG_CONSTITUENTS({etf_ticker},0,CLOSE))=1"
    formula = f'ETP_CONST_WEIGHT("{etf_ticker}",0)'
    return fetch_time_series(universe=universe, formulas=[formula])


if __name__ == "__main__":
    # Single-ticker smoke test only -- see run_smoke_test.py for the full,
    # budget-aware sequence.
    print(get_constituent_weight_single("SAP-DE"))
