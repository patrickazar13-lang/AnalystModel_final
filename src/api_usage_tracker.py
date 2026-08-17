"""
api_usage_tracker.py
=====================
Persisted tracking of REAL FactSet Formula API usage (requests + points),
across ALL runs/days -- not just one script invocation.

WHY THIS EXISTS: master_pipeline.py's CallBudget class (config.SESSION_CALL_BUDGET)
is an in-memory, single-RUN guardrail -- Patrick's own "how much am I
comfortable spending this session" cap, raised twice already by explicit
choice. It has NO memory of previous runs: if master_pipeline.py --live gets
run several times in a day (or across days, e.g. working through a 52-ticker
list), each run independently starts counting from 0 again, unaware of what
prior runs already spent. That's a real gap once you're pulling more than a
couple of tickers, because FactSet enforces its OWN hard limits server-side,
completely independent of anything in this codebase (pasted by Patrick
verbatim from FactSet's docs):

    100 API REQUESTS / day
    1,000 API REQUESTS / month
    100,000 API POINTS / month
    5 requests/sec, 5 concurrent (not a concern here -- this pipeline only
    ever makes one call at a time, sequentially)

THE CRITICAL DISTINCTION (also from that doc): an API POINT = number of
identifiers in ONE request (cost doesn't depend on how many formulas are
bundled in); an API REQUEST = one HTTP call, full stop. Because every call
in this project targets a SINGLE ticker (never a multi-name universe
call), points and requests are numerically IDENTICAL here -- each call
costs exactly 1 of each. That means the 100,000-points/month ceiling is
nowhere close to binding for this project's usage pattern, but the
100-requests/DAY ceiling is: at 25 calls/ticker (config.LIVE_N_QUARTERS=12),
that's only ~4 tickers PER DAY before hitting FactSet's real, server-
enforced daily wall -- a much tighter, and much more immediate, constraint
than the 150-point SESSION_CALL_BUDGET number suggests on its own, and one
the old single-run counter had no way to see.

This module logs every call to data/factset_usage_log.json (one line per
call: date, requests, points, label) so today's/this month's REAL totals
are visible across every run, and check_and_record() refuses to let a new
call proceed if it would breach FactSet's actual limits -- not just the
self-imposed session cap. CallBudget.spend() (master_pipeline.py) calls
this on every real spend, in addition to its own existing check.

CAVEAT (read before trusting the numbers): this log only knows about calls
made AFTER this file existed. Any --live pulls run earlier (AAPL-US,
KLAC-US) aren't in it unless manually backfilled -- see backfill_usage_log()
below.
"""

from __future__ import annotations

import json
import os
from datetime import date

DAILY_REQUEST_LIMIT = 100
MONTHLY_REQUEST_LIMIT = 1000
MONTHLY_POINTS_LIMIT = 100_000

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_LOG_PATH = os.path.join(_DATA_DIR, "factset_usage_log.json")


def _load_log() -> list:
    if os.path.exists(_LOG_PATH):
        with open(_LOG_PATH) as f:
            return json.load(f)
    return []


def _save_log(entries: list) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_LOG_PATH, "w") as f:
        json.dump(entries, f, indent=2)


def _today_str() -> str:
    return date.today().isoformat()


def _month_str() -> str:
    return _today_str()[:7]  # "YYYY-MM"


def usage_summary() -> dict:
    """
    Real cumulative usage against FactSet's own enforced limits, computed
    from EVERY call ever logged (across all past runs/days) -- not just
    this process's in-memory counter.
    """
    entries = _load_log()
    today, month = _today_str(), _month_str()
    requests_today = sum(e["requests"] for e in entries if e["date"] == today)
    requests_month = sum(e["requests"] for e in entries if e["date"][:7] == month)
    points_month = sum(e["points"] for e in entries if e["date"][:7] == month)
    return {
        "requests_today": requests_today,
        "requests_today_limit": DAILY_REQUEST_LIMIT,
        "requests_today_remaining": DAILY_REQUEST_LIMIT - requests_today,
        "requests_month": requests_month,
        "requests_month_limit": MONTHLY_REQUEST_LIMIT,
        "points_month": points_month,
        "points_month_limit": MONTHLY_POINTS_LIMIT,
    }


def check_and_record(n_requests: int, n_points: int, label: str) -> None:
    """
    Call BEFORE each real API call. Raises RuntimeError if adding
    n_requests/n_points would breach any of FactSet's real, server-enforced
    limits -- otherwise logs the call (so it's counted whether or not the
    call itself later succeeds -- deliberately conservative: overcounting
    is safe, undercounting risks tripping FactSet's own hard stop) and lets
    it proceed.
    """
    summary = usage_summary()
    if summary["requests_today"] + n_requests > DAILY_REQUEST_LIMIT:
        raise RuntimeError(
            f"FactSet DAILY request limit would be exceeded: "
            f"{summary['requests_today']} already used today + {n_requests} more "
            f"> {DAILY_REQUEST_LIMIT}/day. This resets at midnight (FactSet's server "
            f"time) -- wait until then, or pull fewer tickers/quarters right now."
        )
    if summary["requests_month"] + n_requests > MONTHLY_REQUEST_LIMIT:
        raise RuntimeError(
            f"FactSet MONTHLY request limit would be exceeded: "
            f"{summary['requests_month']} already used this month + {n_requests} more "
            f"> {MONTHLY_REQUEST_LIMIT}/month."
        )
    if summary["points_month"] + n_points > MONTHLY_POINTS_LIMIT:
        raise RuntimeError(
            f"FactSet MONTHLY points limit would be exceeded: "
            f"{summary['points_month']} already used this month + {n_points} more "
            f"> {MONTHLY_POINTS_LIMIT}/month."
        )

    entries = _load_log()
    entries.append({"date": _today_str(), "requests": n_requests, "points": n_points, "label": label})
    _save_log(entries)


def backfill_usage_log(n_requests: int, n_points: int, label: str, on_date: "str | None" = None) -> None:
    """
    Manually register calls that were made BEFORE this tracker existed (or
    outside of it) so today's/this month's totals are accurate. on_date
    defaults to today; pass 'YYYY-MM-DD' for a past date. Does NOT run the
    limit check (these calls already happened) -- just records them.
    """
    entries = _load_log()
    entries.append({"date": on_date or _today_str(), "requests": n_requests, "points": n_points, "label": label})
    _save_log(entries)


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(usage_summary(), indent=2))
