"""
Web research for a bet — recent news headlines + automatic injury/suspension flags.

No paid key: we read Google News' public RSS search feed, which returns real, current
headlines for any query. We pull the last few days for the matchup (and a player, for
player props), then scan for injury / suspension / lineup language and surface it as a
risk signal. It does NOT pretend to "understand" the news — it shows you the real
headlines and flags the words that move a bet, so you read the ones that matter.
"""
from __future__ import annotations
import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional

import httpx

_UA = {"User-Agent": "Mozilla/5.0 (AlphaMarketsAI research)"}
_FEED = "https://news.google.com/rss/search"
_CACHE: Dict[str, tuple] = {}     # query -> (ts, parsed items)
_TTL = 1800                       # 30 min — headlines don't change minute to minute

# Words that signal a bet-moving availability story. Deliberately specific: bare "knock"
# ("knock out Croatia"), bare "return" ("return to the Azteca") and bare "fitness" flagged
# half the slate as injury news, which watered the signal down to noise.
_RISK_RE = re.compile(
    r"\b(injur\w*|doubt\w*|ruled out|out for|sidelin\w*|suspend\w*|banned|"
    r"hamstring|strain\w*|fitness (?:test|doubt|concern|race)|withdraw\w*|"
    r"miss(?:es|ed)? (?:the (?:match|game|clash)|out)|to miss\b|left out|not in the squad|"
    r"knocks?\b(?!\s*out)|fit again|back in train\w*|"
    r"return\w* (?:from|to) (?:injury|training|the squad|full fitness))\b", re.I)


async def _fetch(query: str) -> List[Dict]:
    cached = _CACHE.get(query)
    now = time.time()
    if cached and now - cached[0] < _TTL:
        return cached[1]
    params = {"q": f"{query} when:5d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    items: List[Dict] = []
    try:
        async with httpx.AsyncClient(timeout=15, headers=_UA) as c:
            r = await c.get(_FEED, params=params)
            r.raise_for_status()
            root = ET.fromstring(r.text)
        for it in root.findall(".//item"):
            title = html.unescape(it.findtext("title") or "").strip()
            if not title:
                continue
            src_el = it.find("source")
            pub = it.findtext("pubDate") or ""
            try:
                dt = parsedate_to_datetime(pub)
                age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            except (TypeError, ValueError):
                age_h = 999
            items.append({
                "title": title,
                "source": (src_el.text if src_el is not None else "") or "",
                "url": it.findtext("link") or "",
                "age_hours": round(age_h, 1),
                "risk": bool(_RISK_RE.search(title)),
            })
        items.sort(key=lambda x: x["age_hours"])
    except Exception as exc:
        print(f"[news_research] '{query}' failed: {exc}")
        items = []
    _CACHE[query] = (now, items)
    return items


async def research(home: str, away: str, player: Optional[str] = None,
                   limit: int = 6) -> Dict:
    """Recent headlines for a matchup (+ a player for props), with injury/availability flags."""
    match_items = await _fetch(f"{home} {away} World Cup")
    player_items = await _fetch(f"{player} {home} {away}") if player else []

    alerts = [i for i in (match_items + player_items) if i["risk"]][:6]
    headlines = (player_items[:limit] if player else []) + match_items[:limit]
    # de-dupe by title, keep order (player news first)
    seen, deduped = set(), []
    for h in headlines:
        if h["title"] in seen:
            continue
        seen.add(h["title"])
        deduped.append(h)

    if not deduped:
        summary = "No recent news found — treat the model + book read as your only signal."
    elif alerts:
        summary = (f"⚠️ {len(alerts)} availability/injury headline(s) in the last few days — "
                   f"read these before you place; a lineup change can sink this bet.")
    else:
        summary = "Recent coverage found, nothing flagging injuries or suspensions right now."

    return {
        "home": home, "away": away, "player": player,
        "summary": summary,
        "injury_alerts": alerts,
        "headlines": deduped[:limit + (limit if player else 0)],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def games_risk(games) -> Dict:
    """Injury/availability risk per matchup, FOR THE PARLAY ENGINE (this is where the news
    actually changes the numbers, not just the display). Reuses the same 30-min cached Google
    News fetches as /api/research, so a parlay click costs at most one feed hit per new game.
    Only headlines from the last 48h count — week-old 'doubt' stories are usually resolved.
    Returns {(home, away): {"alerts": [headline, ...]}} for flagged games only."""
    import asyncio
    games = list(games)
    results = await asyncio.gather(*[research(h, a, limit=3) for h, a in games],
                                   return_exceptions=True)
    out: Dict = {}
    for (h, a), r in zip(games, results):
        if isinstance(r, BaseException):
            print(f"[news_research] risk check {h} v {a} failed: {r}")
            continue
        alerts = [i["title"] for i in (r.get("injury_alerts") or []) if i.get("age_hours", 999) <= 48]
        if alerts:
            out[(h, a)] = {"alerts": alerts}
    return out
