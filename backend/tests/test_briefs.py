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


def test_without_a_key_briefs_are_off(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert asyncio.run(briefs.generate(CARDS)) == {"enabled": False, "reason": "No ANTHROPIC_API_KEY configured."}


def test_prompt_carries_every_field_and_the_citation_rule():
    prompt = briefs.build_prompt(CARDS[0])
    for needle in ["Bengals at Commanders", "0.31", "0.25", "4.6", "QB injury not priced", "No weather forecast.",
                   "WATCHING", "Only 14 games settled"]:
        assert needle in prompt
    assert "[brackets]" in prompt and "insufficient data" in prompt.lower()
    assert "do not recommend a stake" in prompt.lower()


def test_generates_one_brief_per_card_with_the_default_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient()
    monkeypatch.setattr(briefs, "_client", lambda: fake)
    out = asyncio.run(briefs.generate(CARDS))
    assert out["enabled"] is True and [b["card_id"] for b in out["briefs"]] == ["c1", "c2"]
    assert fake.last["model"] == "claude-opus-5-5"
    assert fake.last["fallbacks"] == "default"


def test_api_error_returns_cleanly_without_raising(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(briefs, "_client", lambda: FakeClient(fail=True))
    out = asyncio.run(briefs.generate(CARDS))
    assert out["enabled"] is True and out["briefs"] == [] and "overloaded" in out["error"]


def test_cache_hit_avoids_a_second_call(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient()
    monkeypatch.setattr(briefs, "_client", lambda: fake)
    asyncio.run(briefs.generate(CARDS))
    asyncio.run(briefs.generate(CARDS))
    assert fake.calls == 2          # two cards, one call each, once


def test_refusal_is_reported_not_shown_as_a_brief(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient()

    async def refuse(**kw):
        return SimpleNamespace(stop_reason="refusal", content=[])
    fake.beta.messages.create = refuse
    monkeypatch.setattr(briefs, "_client", lambda: fake)
    out = asyncio.run(briefs.generate(CARDS[:1]))
    assert out["briefs"] == [] and "declined" in out["error"]
