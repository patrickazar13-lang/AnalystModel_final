"""
Probe: does FE_BROKER_ESTIMATE support a BKR_CODE (stable broker identifier)
item, as a more reliable alternative to the free-text BKR_NAME we already
confirmed working?

Patrick's candidate, found in an old workbook:
    FE_BROKER_ESTIMATE(SNAP,EPS,BKR_CODE,ANN_ROLL,0,0,,'')

Caution: this has the EXACT same shape (item,ANN_ROLL/QTR_R,period,0,,date)
as formulas from AMUNDI_FINANCIALS.xlsm that turned out to be self-flagged
"VERIFY" (never live-tested) in that workbook's own README, and a similar
periodicity-argument pattern already failed with "not valid" errors in
round 3 of this project's formula discovery (see estimates_via_formula.py's
history, now archived). So this probe tests Patrick's candidate AS-IS, but
ALSO tests our own adaptation of the pattern already proven working for
BKR_NAME/EST_VALUE/AN_NAME (same blank-arg SNAP shape, just swapping in
BKR_CODE) -- whichever comes back with a real value (not an error or a
silent None) tells us which syntax is actually correct.

Still exactly 1 API point (single ticker, multiple formulas bundled).

Run with:
    py -3.11 probe_bkr_code.py
"""

from src.factset_client import fetch_time_series
from src.config import TEST_TICKER

CALLS_MADE = 0


def main():
    global CALLS_MADE
    date = "20250414"  # same snapshot date already proven to return real EPS estimates

    formulas = [
        "FE_BROKER_ESTIMATE(SNAP,EPS,BKR_CODE,ANN_ROLL,0,0,,'')",   # Patrick's candidate, as-is
        f"FE_BROKER_ESTIMATE(SNAP,EPS,BKR_CODE,,,'{date}')",         # our adaptation of the CONFIRMED pattern
        f"FE_BROKER_ESTIMATE(SNAP,EPS,BKR_NAME,,,'{date}')",         # anchor/sanity check -- already proven working
        f"FE_BROKER_ESTIMATE(SNAP,EPS,AN_NAME,,,'{date}')",          # anchor -- lets us match code/name/analyst by row
    ]

    print(f"--- Probing BKR_CODE for {TEST_TICKER} ---")
    CALLS_MADE += 1
    result = fetch_time_series(ids=[TEST_TICKER], formulas=formulas)

    print("\nRaw response:")
    print(result)

    print("\n--- Quick per-formula summary ---")
    data = result.get("data", [])
    by_formula = {}
    for row in data:
        for key, value in row.items():
            if key == "requestId":
                continue
            by_formula.setdefault(key, []).append(value)

    for formula, values in by_formula.items():
        sample = values[:10]
        print(f"{formula}\n  -> {len(values)} row(s), sample: {sample}\n")

    print(f"Total API calls made this run: {CALLS_MADE}")


if __name__ == "__main__":
    main()
