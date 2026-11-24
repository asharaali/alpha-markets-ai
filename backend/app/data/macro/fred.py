"""FRED (St. Louis Fed) series via the public CSV download — no key."""
from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass
from typing import List, Optional, Tuple

from app.core.http import make_client

CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# id: (label, group, transform) — "index" series report MoM and YoY %, "level" report change.
SERIES = {
    "DGS3MO": ("3-Month", "rates", "level"), "DGS2": ("2-Year", "rates", "level"),
    "DGS5": ("5-Year", "rates", "level"), "DGS10": ("10-Year", "rates", "level"),
    "DGS30": ("30-Year", "rates", "level"), "DFEDTARU": ("Fed funds (upper)", "rates", "level"),
    "CPIAUCSL": ("CPI", "inflation", "index"), "CPILFESL": ("Core CPI", "inflation", "index"),
    "PCEPI": ("PCE", "inflation", "index"), "PCEPILFE": ("Core PCE", "inflation", "index"),
    "PPIFIS": ("PPI", "inflation", "index"),
    "UNRATE": ("Unemployment", "jobs_growth", "level"),
    "A191RL1Q225SBEA": ("Real GDP growth (q/q ann.)", "jobs_growth", "level"),
    "SP500": ("S&P 500 (FRED close)", "fallback", "level"),
    "DCOILWTICO": ("WTI crude (FRED close)", "fallback", "level"),
}


@dataclass
class Indicator:
    id: str
    label: str
    group: str
    value: float
    prev: Optional[float]
    change: Optional[float]
    mom: Optional[float]
    yoy: Optional[float]
    as_of: str
    kind: str = "print"
    source: str = "FRED"

    def to_dict(self):
        return asdict(self)


def parse_csv(text: str) -> List[Tuple[str, float]]:
    rows = []
    for rec in list(csv.reader(io.StringIO(text)))[1:]:
        if len(rec) < 2:
            continue
        try:
            rows.append((rec[0], float(rec[1])))
        except ValueError:
            continue          # FRED writes "." for days with no observation
    return rows


def indicator(series: str, rows: List[Tuple[str, float]]) -> Indicator:
    label, group, transform = SERIES[series]
    date, value = rows[-1]
    prev = rows[-2][1] if len(rows) > 1 else None
    mom = yoy = None
    if transform == "index":
        if prev:
            mom = round((value / prev - 1) * 100, 4)
        if len(rows) > 12 and rows[-13][1]:
            yoy = round((value / rows[-13][1] - 1) * 100, 4)
    return Indicator(id=series, label=label, group=group, value=value, prev=prev,
                     change=None if prev is None else round(value - prev, 4),
                     mom=mom, yoy=yoy, as_of=date)


async def fetch(series: str) -> Indicator:
    # FRED rejects some custom User-Agents; a browser-like one is accepted.
    async with make_client(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as client:
        r = await client.get(CSV_URL, params={"id": series})
        r.raise_for_status()
        rows = parse_csv(r.text)
        if not rows:
            raise RuntimeError(f"FRED returned no observations for {series}")
        return indicator(series, rows)
