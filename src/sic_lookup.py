"""
sic_lookup.py
==============
Automatic SIC-code lookup, replacing the old fully-manual
config.TICKER_SIC_OVERRIDES-only workflow (that dict required looking a
ticker up on SEC EDGAR by hand every single time -- exactly what happened
for KLAC-US).

NOT a FactSet call -- this hits SEC EDGAR's own free, public, unauthenticated
JSON endpoints (no API key, no relationship to config.SESSION_CALL_BUDGET at
all):

  1. https://www.sec.gov/files/company_tickers.json
     -- one static file mapping every US-listed ticker -> CIK. ~10k entries.
  2. https://data.sec.gov/submissions/CIK{10-digit}.json
     -- per-company filing profile, includes "sic" and "sicDescription".

Both endpoints are confirmed working (verified against AAPL-US -> 3571 and
KLAC-US -> 3827, matching the values that were previously hand-entered into
TICKER_SIC_OVERRIDES from a manual SEC EDGAR lookup).

SEC's fair-access policy requires a descriptive User-Agent identifying the
requester (see https://www.sec.gov/os/webmaster-faq#developers) -- NOT an
API key, just an honest contact string. Both files are cached to disk
(data/sec_ticker_to_cik_cache.json, data/sic_code_cache.json) so repeat runs
for the same ticker don't hit the network again.

LIMITATION: only covers companies that file with the SEC (US domestic
filers and foreign private issuers that still show up in this dataset).
config.TICKER_SIC_OVERRIDES is kept as a manual override that's checked
FIRST -- use it for anything this lookup can't resolve, or if you ever find
SEC's classification disagrees with what you want to use.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

_HEADERS = {"User-Agent": "Analyst Model Research (contact: pazar.ieu2022@student.ie.edu)"}
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_TICKER_MAP_CACHE = os.path.join(_DATA_DIR, "sec_ticker_to_cik_cache.json")
_SIC_CACHE = os.path.join(_DATA_DIR, "sic_code_cache.json")

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _load_cache(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save_cache(path: str, data: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _factset_ticker_to_symbol(factset_ticker: str) -> str:
    """'KLAC-US' -> 'KLAC'. Strips any trailing '-XX' exchange suffix."""
    return re.sub(r"-[A-Z]{2}$", "", factset_ticker.upper())


def _ticker_to_cik(symbol: str) -> "int | None":
    cache = _load_cache(_TICKER_MAP_CACHE)
    if symbol in cache:
        return cache[symbol]

    try:
        raw = _fetch_json(_TICKER_MAP_URL)
    except (urllib.error.URLError, TimeoutError, Exception) as e:  # noqa: BLE001
        print(f"[sic_lookup] WARNING: couldn't fetch SEC ticker map ({e}) -- "
              f"can't auto-resolve SIC codes right now.")
        return None

    full_map = {v["ticker"].upper(): v["cik_str"] for v in raw.values()}
    _save_cache(_TICKER_MAP_CACHE, full_map)
    return full_map.get(symbol)


def get_sic_code(factset_ticker: str) -> dict:
    """
    Returns {"sic_code": int|None, "sic_description": str|None, "source": str}.

    source is one of:
      "cache"        -- already looked this ticker up before, read from disk
      "sec_edgar"     -- fresh lookup, just cached for next time
      "not_found"     -- SEC EDGAR has no record for this ticker/symbol
      "network_error" -- couldn't reach SEC EDGAR at all (offline, etc.)

    0 FactSet API calls either way -- this never touches CallBudget.
    """
    cache = _load_cache(_SIC_CACHE)
    if factset_ticker in cache:
        entry = dict(cache[factset_ticker])
        entry["source"] = "cache"
        return entry

    symbol = _factset_ticker_to_symbol(factset_ticker)
    cik = _ticker_to_cik(symbol)
    if cik is None:
        return {"sic_code": None, "sic_description": None, "source": "not_found"}

    try:
        sub = _fetch_json(_SUBMISSIONS_URL.format(cik=cik))
    except (urllib.error.URLError, TimeoutError, Exception) as e:  # noqa: BLE001
        print(f"[sic_lookup] WARNING: couldn't fetch SEC submissions data for "
              f"{factset_ticker} ({e}).")
        return {"sic_code": None, "sic_description": None, "source": "network_error"}

    sic_raw = sub.get("sic")
    sic_code = int(sic_raw) if sic_raw not in (None, "") else None
    result = {"sic_code": sic_code, "sic_description": sub.get("sicDescription")}

    cache[factset_ticker] = result
    _save_cache(_SIC_CACHE, cache)

    result = dict(result)
    result["source"] = "sec_edgar"
    return result


if __name__ == "__main__":
    # Quick self-check, 0 API calls (SEC EDGAR only).
    for t in ("AAPL-US", "KLAC-US"):
        print(t, "->", get_sic_code(t))
