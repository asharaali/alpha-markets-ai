"""Research briefs: a short note per board card.

Default (BRIEFS_MODE unset or "template"): written by code from the card's own fields. Free,
no key, works in the demo, and cannot invent anything, which is the whole point of a brief.

Optional (BRIEFS_MODE=ai and ANTHROPIC_API_KEY set): Claude writes it instead. Each brief sees one card's JSON and nothing else. It must cite the field behind every claim
in [brackets], say "insufficient data" rather than guess, and never suggest a stake — sizing
belongs to the evidence gate. Off unless ANTHROPIC_API_KEY is set. Failures return a reason,
never a 500. Refusals use the server-side fallback; a refusal that survives it is reported.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List

MODEL = "claude-opus-5-5"
CACHE_SECONDS = 6 * 3600
_CACHE: Dict[str, Any] = {}

_FIELDS = ("matchup", "selection", "side", "hit", "price", "edge_cents", "reasons", "missing", "evidence")

_INSTRUCTIONS = """You are writing a research note for one trader about one prediction-market contract.
Use ONLY the data below. Write at most 120 words of plain prose.
Every factual claim must cite the data field it comes from in [brackets], e.g. [hit], [evidence.reason].
Where the data does not support a point, write "insufficient data" instead of guessing.
Do not recommend a stake or a bet size, and do not tell the reader to buy or sell.
Say plainly what the evidence state means for how much this signal can be trusted.
"""


def build_prompt(card: Dict[str, Any]) -> str:
    data = {k: card.get(k) for k in _FIELDS}
    return f"{_INSTRUCTIONS}\nDATA:\n{json.dumps(data, indent=1)}"


def _client():
    import anthropic
    return anthropic.AsyncAnthropic()


async def _one(client, card: Dict[str, Any]) -> Dict[str, Any]:
    response = await client.beta.messages.create(
        model=MODEL, max_tokens=2000,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        messages=[{"role": "user", "content": build_prompt(card)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined to write this brief")
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    return {"card_id": card.get("id"), "title": f"{card.get('selection')} · {card.get('matchup')}", "text": text}


def _c(p: float) -> str:
    return f"{round(p * 100)}¢"


def template_brief(card: Dict[str, Any]) -> Dict[str, Any]:
    ev = card.get("evidence") or {}
    state = ev.get("state", "WATCHING")
    edge = float(card.get("edge_cents") or 0)
    lines = [
        f"{card.get('selection')} ({str(card.get('side', 'yes')).upper()}) · {card.get('matchup')}",
        f"Hit {round(float(card.get('hit', 0)) * 100)}% vs {_c(float(card.get('price', 0)))} · "
        f"{'+' if edge > 0 else ''}{edge:.1f}¢ after fees.",
    ]
    if card.get("reasons"):
        lines.append("Why: " + "; ".join(card["reasons"]) + ".")
    if card.get("missing"):
        lines.append("Missing: " + " ".join(card["missing"]))
    if 0 < edge < 1:
        lines.append("Thin edge: under 1¢ after fees, so a small price move erases it.")
    lines.append(f"Trust: {state}. {ev.get('reason', '')}".rstrip())
    if state != "PROVEN":
        lines.append("No stake until proven.")
    return {"card_id": card.get("id"), "title": f"{card.get('selection')} · {card.get('matchup')}",
            "text": "\n".join(lines)}


def _templates(cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"enabled": True, "source": "template", "generated_at": time.time(),
            "briefs": [template_brief(c) for c in cards]}


async def generate(cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    if os.environ.get("BRIEFS_MODE", "template").lower() != "ai" or not os.environ.get("ANTHROPIC_API_KEY"):
        return _templates(cards)
    # Keyed on the contracts, not card ids: ids change on every board rebuild, which would
    # turn a 6-hour cache into a fresh paid call every refresh.
    key = "|".join(sorted(f"{c.get('ticker') or c.get('id')}:{c.get('side')}" for c in cards))
    hit = _CACHE.get(key)
    if hit and time.time() - hit["generated_at"] < CACHE_SECONDS:
        return hit
    if not cards:
        return {"enabled": True, "source": "ai", "generated_at": time.time(), "briefs": []}
    client = _client()
    results = await asyncio.gather(*(_one(client, c) for c in cards), return_exceptions=True)
    done = [r for r in results if not isinstance(r, BaseException)]
    errors = [str(r) for r in results if isinstance(r, BaseException)]
    out: Dict[str, Any] = {"enabled": True, "source": "ai", "generated_at": time.time(), "briefs": done}
    if errors:
        out["error"] = f"{len(errors)} brief(s) failed: {errors[0]}"
        if not done and key in _CACHE:
            return {**_CACHE[key], "error": out["error"]}
    else:
        _CACHE[key] = out
    return out
