"""
Thin wrapper around FactSet's Formula API (time-series endpoint) -- same
product/endpoint used by theone-event-study's src/factset_client.py.

We're standardizing on the Formula API for this project (not the separate
FactSet Estimates REST SDK) because:
  - It's the same formula engine the Excel Add-in uses (FE_BROKER_ESTIMATE,
    FG_CONSTITUENTS, ETP_CONST_WEIGHT, P_PRICE, etc. are all valid formula
    strings here).
  - It's the one product confirmed working on this account -- the separate
    fds.sdk.FactSetEstimates Broker Detail endpoint came back 403 "User is
    not authorized for the id/endpoint requested" on theone-event-study,
    while this same Formula API worked fine for FF_EPS_RPT_DATE / P_PRICE.

Credentials come from .env (never hardcoded). Copy .env.example to .env and
fill in FACTSET_USERNAME / FACTSET_API_KEY before running anything.
"""

import base64
import os

import requests
from dotenv import load_dotenv

load_dotenv()

FACTSET_CLIENT_ID = os.getenv("FACTSET_CLIENT_ID")
FACTSET_CLIENT_SECRET = os.getenv("FACTSET_CLIENT_SECRET")

BASE_URL = "https://api.factset.com/formula-api/v1/time-series"


def _auth_header() -> dict:
    if not FACTSET_USERNAME or not FACTSET_API_KEY:
        raise RuntimeError(
            "Missing FACTSET_USERNAME / FACTSET_API_KEY. "
            "Copy .env.example to .env and fill in your real credentials."
        )
    raw = f"{FACTSET_USERNAME}:{FACTSET_API_KEY}".encode("ascii")
    token = base64.b64encode(raw).decode("ascii")
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def fetch_time_series(ids: list = None, universe: str = None, formulas: list = None, flatten: str = "Y") -> dict:
    """
    Calls the Formula API with either an explicit id list or a universe
    expression (e.g. "(FG_CONSTITUENTS(EXSA-DE,0,CLOSE))=1"). Pass exactly
    one of ids / universe.

    IMPORTANT (API Points / your usage cap): if you pass `universe`, the
    number of API points billed equals the number of identifiers that match
    the universe criteria -- for something like the Stoxx 600 ETF that's
    ~600 points in ONE call. Always test with a single ticker in `ids`
    first (as instructed), and only widen to a real `universe` once the
    formula shape is confirmed working.
    """
    if bool(ids) == bool(universe):
        raise ValueError("Pass exactly one of ids= or universe=, not both/neither.")

    payload = {"data": {"formulas": formulas, "flatten": flatten}}
    if ids:
        payload["data"]["ids"] = ids
    else:
        payload["data"]["universe"] = universe

    resp = requests.post(BASE_URL, headers=_auth_header(), json=payload, timeout=30)
    if not resp.ok:
        # Surface FactSet's actual error body -- resp.raise_for_status() alone
        # hides it, and that body usually says exactly what's wrong (bad
        # credentials vs. IP mismatch vs. something else).
        print(f"[factset_client] HTTP {resp.status_code} from FactSet. Response body:")
        print(resp.text)
        _debug_credentials()
    resp.raise_for_status()
    return resp.json()


def _debug_credentials() -> None:
    """
    Prints a SAFE (non-secret-revealing) fingerprint of the credentials
    actually being sent, so we can catch whitespace/truncation/wrong-value
    bugs in .env without ever printing the real key.
    """
    user = FACTSET_USERNAME or "<missing>"
    key = FACTSET_API_KEY or "<missing>"
    key_fingerprint = (
        f"len={len(key)}, starts='{key[:4]}', ends='{key[-4:]}'"
        if key and key != "<missing>"
        else "<missing>"
    )
    print(f"[factset_client] username='{user}' (len={len(user)}), api_key: {key_fingerprint}")
    if user != user.strip():
        print("[factset_client] WARNING: FACTSET_USERNAME has leading/trailing whitespace!")
    if key != key.strip():
        print("[factset_client] WARNING: FACTSET_API_KEY has leading/trailing whitespace!")
