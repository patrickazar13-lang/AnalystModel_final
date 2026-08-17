"""
Fama-French 48-industry classification.

Source: Ken French's Data Library, "48 Industry Portfolios" definitions
(Siccodes48.zip), downloaded 2026-07-30. Free, static reference data --
no FactSet / API calls involved.

Used by aggregation.py to group firms into the 48 industries the paper
(Chhaochharia, Kumar, Rantala, Zhang, 2022) uses for its industry-level
"Analyst Sentiment" measure (Eq. 6/7) and its Long-Short trading strategy.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import Optional

_CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "siccodes48.csv")


@dataclass(frozen=True)
class SicRange:
    industry_num: int
    industry_code: str
    industry_name: str
    sic_start: int
    sic_end: int


_RANGES: list[SicRange] = []


def _load() -> None:
    if _RANGES:
        return
    with open(_CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            _RANGES.append(
                SicRange(
                    industry_num=int(row["industry_num"]),
                    industry_code=row["industry_code"],
                    industry_name=row["industry_name"],
                    sic_start=int(row["sic_start"]),
                    sic_end=int(row["sic_end"]),
                )
            )


def sic_to_industry(sic_code) -> Optional[SicRange]:
    """
    Map a 4-digit SIC code (int or str, e.g. 3674 for AAPL's semiconductor
    SIC) to one of the 48 Fama-French industries. Returns None if the SIC
    code doesn't fall in any defined range (French's scheme intentionally
    leaves some SIC codes unclassified -- those fall into industry 48
    "Other" only if explicitly listed, otherwise they're just unmapped).
    """
    _load()
    sic = int(sic_code)
    for r in _RANGES:
        if r.sic_start <= sic <= r.sic_end:
            return r
    return None


def all_industries() -> list[str]:
    _load()
    seen = {}
    for r in _RANGES:
        seen[r.industry_num] = r.industry_name
    return [seen[k] for k in sorted(seen)]


def code_to_name_map() -> dict:
    """
    {'Agric': 'Agriculture', 'Chems': 'Chemicals', 'Comps': 'Computers', ...}
    -- 48 entries, built from siccodes48.csv. Needed because Ken French's
    48-Industry-Portfolios return files are keyed by industry CODE, while
    this project's pipeline carries the industry NAME.
    """
    _load()
    return {r.industry_code: r.industry_name for r in _RANGES}


def name_to_code_map() -> dict:
    """Inverse of code_to_name_map(). Names are unique (verified: 48 of each)."""
    return {name: code for code, name in code_to_name_map().items()}


def code_for_name(name) -> Optional[str]:
    """industry_name -> industry_code, or None when the name isn't one of the 48
    (e.g. 'UNMAPPED' from add_ff48_industry's fallback)."""
    return name_to_code_map().get(name)


if __name__ == "__main__":
    # Quick self-check, no API calls.
    _load()
    print(f"Loaded {len(_RANGES)} SIC ranges across {len(all_industries())} industries.")
    # AAPL's FactSet/GICS-style SIC is 3663 (Radio & TV broadcasting & comms
    # equipment) in some vendors' data, 3674 (semiconductors) in others --
    # this is exactly why the real SIC code must come from FactSet per
    # ticker rather than being hardcoded here.
    for test_sic in (3674, 2820, 6021, 9999):
        match = sic_to_industry(test_sic)
        print(test_sic, "->", match.industry_name if match else "UNMAPPED")
