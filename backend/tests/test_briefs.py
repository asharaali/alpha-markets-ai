"""Research briefs: written only from the board's own data, every claim cited, never a 500."""
import asyncio
from types import SimpleNamespace

import pytest

from app.research import briefs

CARDS = [
    {"id": "c1", "matchup": "Bengals at Commanders", "selection": "Bengals", "side": "yes", "hit": 0.31,
     "price": 0.25, "edge_cents": 4.6, "reasons": ["QB injury not priced"], "missing": ["No weather forecast."],
     "evidence": {"state": "WATCHING", "calls": 14, "clusters": 14, "reason": "Only 14 games settled (needs 40)."}},
    {"id": "c2", "matchup": "Jets at Bills", "selection": "Bills", "side": "yes", "hit": 0.71, "price": 0.66,
     "edge_cents": 3.1, "reasons": ["rest edge"], "missing": [],
     "evidence": {"state": "PROVEN", "calls": 212, "clusters": 60, "reason": "Beats the market on 60 games."}},
]


class FakeClient:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls += 1
        self.last = kwargs
        if self.fail:
            raise RuntimeError("overloaded")
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Brief [hit].")])


@pytest.fixture(autouse=True)
def _fresh_cache():
    briefs._CACHE.clear()
    yield
    briefs._CACHE.clear()


def test_ai_mode_without_a_key_falls_back_to_templates(monkeypatch):
    monkeypatch.setenv("BRIEFS_MODE", "ai")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = asyncio.run(briefs.generate(CARDS))
    assert out["enabled"] is True and out["source"] == "template"


def test_prompt_carries_every_field_and_the_citation_rule():
    prompt = briefs.build_prompt(CARDS[0])
    for needle in ["Bengals at Commanders", "0.31", "0.25", "4.6", "QB injury not priced", "No weather forecast.",
                   "WATCHING", "Only 14 games settled"]:
        assert needle in prompt
    assert "[brackets]" in prompt and "insufficient data" in prompt.lower()
    assert "do not recommend a stake" in prompt.lower()


def test_generates_one_brief_per_card_with_the_default_model(monkeypatch):
    monkeypatch.setenv("BRIEFS_MODE", "ai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient()
    monkeypatch.setattr(briefs, "_client", lambda: fake)
    out = asyncio.run(briefs.generate(CARDS))
    assert out["enabled"] is True and [b["card_id"] for b in out["briefs"]] == ["c1", "c2"]
    assert fake.last["model"] == "claude-opus-5-5"
    assert fake.last["fallbacks"] == "default"


def test_api_error_returns_cleanly_without_raising(monkeypatch):
    monkeypatch.setenv("BRIEFS_MODE", "ai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(briefs, "_client", lambda: FakeClient(fail=True))
    out = asyncio.run(briefs.generate(CARDS))
    assert out["enabled"] is True and out["briefs"] == [] and "overloaded" in out["error"]


def test_cache_hit_avoids_a_second_call(monkeypatch):
    monkeypatch.setenv("BRIEFS_MODE", "ai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient()
    monkeypatch.setattr(briefs, "_client", lambda: fake)
    asyncio.run(briefs.generate(CARDS))
    asyncio.run(briefs.generate(CARDS))
    assert fake.calls == 2          # two cards, one call each, once


def test_refusal_is_reported_not_shown_as_a_brief(monkeypatch):
    monkeypatch.setenv("BRIEFS_MODE", "ai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient()

    async def refuse(**kw):
        return SimpleNamespace(stop_reason="refusal", content=[])
    fake.beta.messages.create = refuse
    monkeypatch.setattr(briefs, "_client", lambda: fake)
    out = asyncio.run(briefs.generate(CARDS[:1]))
    assert out["briefs"] == [] and "declined" in out["error"]


# ── template briefs (the default: free, no key, nothing invented) ─────────────

def test_default_mode_is_template_and_needs_no_key(monkeypatch):
    monkeypatch.delenv("BRIEFS_MODE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = asyncio.run(briefs.generate(CARDS))
    assert out["enabled"] is True and out["source"] == "template"
    assert [b["card_id"] for b in out["briefs"]] == ["c1", "c2"]


def test_template_states_the_numbers_reasons_gaps_and_trust():
    text = briefs.template_brief(CARDS[0])["text"]
    assert "Bengals (YES) · Bengals at Commanders" in text
    assert "31% vs 25¢" in text and "+4.6¢ after fees" in text
    assert "Why: QB injury not priced." in text
    assert "Missing: No weather forecast." in text
    assert "Trust: WATCHING. Only 14 games settled (needs 40)." in text
    assert "No stake until proven." in text


def test_template_for_a_proven_card_says_so_and_has_no_gap_line():
    text = briefs.template_brief(CARDS[1])["text"]
    assert "Trust: PROVEN. Beats the market on 60 games." in text
    assert "Missing" not in text
    assert "No stake until proven." not in text


def test_template_flags_a_thin_edge():
    thin = {**CARDS[0], "edge_cents": 0.6}
    assert "Thin edge: under 1¢ after fees, so a small price move erases it." in briefs.template_brief(thin)["text"]


def test_ai_cache_survives_a_board_rebuild_with_new_card_ids(monkeypatch):
    monkeypatch.setenv("BRIEFS_MODE", "ai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient()
    monkeypatch.setattr(briefs, "_client", lambda: fake)
    first = [{**c, "ticker": f"KX-{i}"} for i, c in enumerate(CARDS)]
    rebuilt = [{**c, "id": c["id"] + "-new"} for c in first]      # same contracts, new ids
    asyncio.run(briefs.generate(first))
    asyncio.run(briefs.generate(rebuilt))
    assert fake.calls == 2
