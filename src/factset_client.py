"""
FactSet Formula API client using OAuth 2.0 Client Credentials.

GitHub Secrets required:

FACTSET_CLIENT_ID
FACTSET_CLIENT_SECRET
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

FACTSET_CLIENT_ID = os.getenv("FACTSET_CLIENT_ID")
FACTSET_CLIENT_SECRET = os.getenv("FACTSET_CLIENT_SECRET")

BASE_URL = "https://api.factset.com/formula-api/v1/time-series"
TOKEN_URL = "https://auth.factset.com/as/token.oauth2"

def _get_access_token():
    if not FACTSET_CLIENT_ID or not FACTSET_CLIENT_SECRET:
        raise RuntimeError(
            "Missing FACTSET_CLIENT_ID or FACTSET_CLIENT_SECRET"
        )

    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(FACTSET_CLIENT_ID, FACTSET_CLIENT_SECRET),
        timeout=30,
    )

    print(
        f"[factset_client] OAuth token endpoint status: {resp.status_code}"
    )

    if not resp.ok:
        print("[factset_client] OAuth token request failed")
        print(resp.text)

    resp.raise_for_status()

    payload = resp.json()

    if "access_token" not in payload:
        raise RuntimeError(
            f"OAuth response missing access_token: {payload}"
        )

    return payload["access_token"]


def _auth_header():
    token = _get_access_token()

    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def fetch_time_series(
    ids=None,
    universe=None,
    formulas=None,
    flatten="Y",
):
    """
    Calls the FactSet Formula API time-series endpoint.

    Pass exactly one of:
      - ids=[...]
      - universe="..."

    Example:

        fetch_time_series(
            ids=["AAPL-US"],
            formulas=["P_PRICE()"]
        )
    """

    if bool(ids) == bool(universe):
        raise ValueError(
            "Pass exactly one of ids= or universe=, not both/neither."
        )

    payload = {
        "data": {
            "formulas": formulas,
            "flatten": flatten,
        }
    }

    if ids:
        payload["data"]["ids"] = ids
    else:
        payload["data"]["universe"] = universe

    resp = requests.post(
        BASE_URL,
        headers=_auth_header(),
        json=payload,
        timeout=30,
    )

    if not resp.ok:
        print(
            f"[factset_client] HTTP {resp.status_code} from FactSet"
        )
        print(resp.text)

    resp.raise_for_status()

    return resp.json()
