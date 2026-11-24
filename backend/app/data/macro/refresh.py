"""Build the Macro page's data on the scheduler and cache it as one JSON file.

The request path (/api/macro) only reads this file. Every source is fetched concurrently with
its failure isolated to its own section, so Yahoo rate-limiting never blanks the yields.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict

from app.config import settings
from app.data.macro import fred, kalshi_macro, quotes, sentiment

PATH = Path(settings.CACHE_DIR) / "macro.json"
RATES = ("DGS3MO", "DGS2", "DGS5", "DGS10", "DGS30", "DFEDTARU")
INFLATION = ("CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "PPIFIS")
JOBS_GROWTH = ("UNRATE", "A191RL1Q225SBEA")
FALLBACK = ("SP500", "DCOILWTICO")


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


async def build() -> Dict[str, Any]:
    series = RATES + INFLATION + JOBS_GROWTH + FALLBACK
    fred_res = dict(zip(series, await asyncio.gather(*(fred.fetch(s) for s in series), return_exceptions=True)))
    symbols = list(quotes.SYMBOLS)
    quote_res = await asyncio.gather(*(quotes.fetch_yahoo(s) for s in symbols), quotes.fetch_btc(),
                                     return_exceptions=True)
    rec_res, fng_res = await asyncio.gather(kalshi_macro.fetch_recession(), sentiment.fetch(), return_exceptions=True)

    sections: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    def group(name, ids):
        items = [fred_res[s].to_dict() for s in ids if not isinstance(fred_res[s], BaseException)]
        failed = [f"{s}: {_err(fred_res[s])}" for s in ids if isinstance(fred_res[s], BaseException)]
        sections[name] = {"items": items, "source": "FRED", "kind": "print"}
        if failed:
            errors[name] = "; ".join(failed)

    group("rates", RATES)
    group("inflation", INFLATION)
    group("jobs_growth", JOBS_GROWTH)

    upper = fred_res["DFEDTARU"]
    if isinstance(upper, BaseException):
        errors["fed"] = "Fed odds need today's upper bound from FRED, which failed."
    else:
        try:
            sections["fed"] = (await kalshi_macro.fetch_fed(upper.value)).to_dict()
        except Exception as exc:   # noqa: BLE001 - isolate to this section
            errors["fed"] = _err(exc)

    good = [q.to_dict() for q in quote_res if not isinstance(q, BaseException)]
    bad = [f"{s}: {_err(q)}" for s, q in zip(symbols + ["BTC"], quote_res) if isinstance(q, BaseException)]
    fallback = [fred_res[s].to_dict() for s in FALLBACK if not isinstance(fred_res[s], BaseException)]
    sections["markets"] = {"quotes": good, "fallback": fallback if bad else []}
    if bad:
        errors["markets"] = "; ".join(bad)

    if isinstance(rec_res, BaseException):
        errors["recession"] = _err(rec_res)
    else:
        sections["recession"] = rec_res.to_dict()
    if isinstance(fng_res, BaseException):
        errors["sentiment"] = _err(fng_res)
    else:
        sections["sentiment"] = fng_res.to_dict()

    return {"ready": True, "built_at": time.time(), "sections": sections, "errors": errors}


async def refresh() -> Dict[str, Any]:
    """Scheduler job: build and write atomically. Returns a short summary for /api/status/jobs."""
    blob = await build()
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(f".part{os.getpid()}")
    tmp.write_text(json.dumps(blob))
    tmp.replace(PATH)
    return {"sections": len(blob["sections"]), "errors": list(blob["errors"])}


def read() -> Dict[str, Any]:
    try:
        return json.loads(PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ready": False}
