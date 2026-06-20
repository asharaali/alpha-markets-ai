"""
Weather model calibration — the "trust it first" machinery.

Like the soccer reality factor, this needs accumulated data to mean anything: it snapshots
the model's forecast for each city/day as we price the board, then — once that day is over —
pulls the ACTUAL observed high from the official NWS station and scores the forecast. Over a
week or two it tells us, with real numbers, whether our forecast error (sigma) assumption is
right per lead time, so we only trust the edge once it's earned.
"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import httpx

from app.config import settings
from app import weather_model

_LOG = Path(settings.DATA_DIR) / "weather_forecasts.json"
_UA = {"User-Agent": "AlphaMarketsAI/1.0 (calibration)"}

# Official Kalshi settlement stations (ASOS) per high-temp series.
STATIONS = {
    "KXHIGHNY": "KNYC", "KXHIGHCHI": "KMDW", "KXHIGHMIA": "KMIA", "KXHIGHLAX": "KLAX",
    "KXHIGHDEN": "KDEN", "KXHIGHAUS": "KAUS", "KXHIGHPHIL": "KPHL",
}


def _load() -> Dict:
    if _LOG.exists():
        try:
            return json.loads(_LOG.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save(d: Dict):
    _LOG.write_text(json.dumps(d, indent=2))


def log_forecasts(rows: List[Dict]) -> None:
    """Snapshot the forecast for each (city, date) at its current lead. Idempotent per lead."""
    d = _load()
    seen = {}
    for r in rows:
        key = f"{r['series']}|{r['date']}"
        lead = str(r["lead_days"])
        if (key, lead) in seen:
            continue
        seen[(key, lead)] = True
        entry = d.setdefault(key, {"series": r["series"], "city": r["city"],
                                   "date": r["date"], "leads": {}, "observed": None})
        entry["leads"].setdefault(lead, r["forecast_high_f"])
    _save(d)


async def _observed_high(client, station: str, day: str):
    """Actual observed daily high (°F) at an NWS station for YYYY-MM-DD. None if unavailable."""
    try:
        r = await client.get(f"https://api.weather.gov/stations/{station}/observations",
                             params={"start": f"{day}T00:00:00Z", "end": f"{day}T23:59:59Z", "limit": 500})
        feats = r.json().get("features", [])
    except Exception:
        return None
    hi = None
    for o in feats:
        t = (o.get("properties") or {}).get("temperature", {}).get("value")
        if t is not None:
            f = t * 9 / 5 + 32
            hi = f if hi is None else max(hi, f)
    return round(hi, 1) if hi is not None else None


async def calibration_report() -> Dict:
    """Score every logged forecast whose day has passed against the observed high."""
    d = _load()
    today = datetime.now(timezone.utc).date()
    # Fill in observed highs for finished, not-yet-scored days.
    async with httpx.AsyncClient(timeout=25, headers=_UA) as client:
        for key, e in d.items():
            if e.get("observed") is not None:
                continue
            try:
                tgt = date.fromisoformat(e["date"])
            except ValueError:
                continue
            if tgt >= today:
                continue  # not finished yet
            station = STATIONS.get(e["series"])
            if station:
                e["observed"] = await _observed_high(client, station, e["date"])
    _save(d)

    # Aggregate absolute forecast error by lead time.
    by_lead = defaultdict(list)
    scored = 0
    for e in d.values():
        obs = e.get("observed")
        if obs is None:
            continue
        for lead, fc in e["leads"].items():
            by_lead[int(lead)].append(abs(fc - obs))
            scored += 1
    leads = []
    for lead in sorted(by_lead):
        errs = by_lead[lead]
        mae = sum(errs) / len(errs)
        leads.append({
            "lead_days": lead, "n": len(errs), "mae_f": round(mae, 2),
            "assumed_sigma_f": weather_model.sigma_for_lead(lead),
        })
    return {
        "scored_points": scored,
        "tracked_days": len([e for e in d.values() if e.get("observed") is not None]),
        "by_lead": leads,
        "status": ("Calibrating — need several finished days before the error stats are trustworthy."
                   if scored < 20 else "Enough data to read forecast accuracy by lead time."),
    }
