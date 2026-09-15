"""Picks that make sense: correct uncertainty, both sides of a contract, no losing cards,
no repeated ladder rungs, and a high-win-rate lane that still refuses overpriced bets."""
from __future__ import annotations

import pytest

from app.core.types import Confidence, MarketType, Side
from app.models import calibration
from app.models import game_model
from app.strategies import recommendation as rec
from app.strategies import sides

from tests.conftest import make_game, make_projection, make_signal


class TestUncertaintyCombinesInQuadrature:
    def test_extra_sigma_is_not_added_linearly(self, monkeypatch):
        game = make_game()
        artifact = calibration.GameModelArtifact.__new__(calibration.GameModelArtifact)
        artifact.margin_coefficients = []
        artifact.total_coefficients = []
        artifact.margin_sigma = 13.12
        artifact.total_sigma = 13.24
        artifact.margin_key_profile = {}
        artifact.total_key_profile = {}
        monkeypatch.setattr(game_model, "margin_predictors", lambda *a, **k: [])
        monkeypatch.setattr(game_model, "total_predictors", lambda *a, **k: [])
        monkeypatch.setattr(game_model, "_driver_rows", lambda *a, **k: [])

        class _RS:
            def confidence(self):
                return 0.8

        proj = game_model.project(game, _RS(), artifact, extra_margin_sigma=2.83)
        assert proj.margin_sigma == pytest.approx((13.12 ** 2 + 2.83 ** 2) ** 0.5, abs=1e-3)
        assert proj.margin_sigma < 13.12 + 2.83


class TestNoSide:
    def test_spread_no_is_taking_the_points(self):
        game = make_game(home="SF", away="MIA")
        yes = make_signal("SPREAD-MIA3", game_id=game.game_id, market_type=MarketType.SPREAD,
                          team="MIA", line=2.5, model_prob=0.107, market_prob=0.085,
                          cost=0.09, label="Dolphins by more than 2.5")
        no = sides.no_side(yes, game)
        assert no is not None
        assert no.quote.side is Side.NO
        assert no.features["fair_prob"] == pytest.approx(1 - 0.107, abs=1e-4)
        # NO costs one minus the YES bid.
        assert no.quote.cost == pytest.approx(1 - 0.08, abs=1e-6)
        assert "49ers" in no.selection and "+2.5" in no.selection

    def test_total_no_is_the_under(self):
        game = make_game()
        yes = make_signal("TOTAL-45", game_id=game.game_id, market_type=MarketType.TOTAL,
                          line=45.5, model_prob=0.40, market_prob=0.45, cost=0.46,
                          label="Over 45.5")
        no = sides.no_side(yes, game)
        assert no.selection == "Under 45.5"
        assert no.features["fair_prob"] == pytest.approx(0.60, abs=1e-4)

    def test_moneyline_has_no_mirror(self):
        game = make_game()
        yes = make_signal("ML-KC", game_id=game.game_id, team="KC")
        assert sides.no_side(yes, game) is None, (
            "NO on one team's moneyline is the other team's YES, which is already priced")


def _card(ticker, **kw):
    signal = make_signal(ticker, **kw)
    return rec.build(signal, matchup="Bills at Chiefs", kickoff="2026-09-13T17:00:00Z")


class TestBoardDiscipline:
    def test_negative_after_fee_cards_are_dropped(self):
        good = _card("A", model_prob=0.62, market_prob=0.56, cost=0.56)
        bad = _card("B", model_prob=0.565, market_prob=0.56, cost=0.56)
        assert bad.ev_after_fees < 0
        kept = rec.profitable([good, bad])
        assert [c.ticker for c in kept] == ["A"]

    def test_ladder_rungs_on_the_same_bet_collapse_to_one(self):
        cards = [_card(f"CLE{n}", market_type=MarketType.SPREAD, team="CLE", line=n + 0.5,
                       model_prob=0.30, market_prob=0.25, cost=0.25) for n in (3, 4, 5, 6)]
        kept = rec.one_per_bet(cards)
        assert len(kept) == 1


class TestHighWinRate:
    def test_likely_and_fairly_priced_qualifies(self):
        card = _card("FAV", model_prob=0.82, market_prob=0.78, cost=0.78)
        assert rec.is_high_win_rate(card)

    def test_likely_but_overpriced_is_refused(self):
        card = _card("PRICEY", model_prob=0.80, market_prob=0.82, cost=0.82)
        assert not rec.is_high_win_rate(card), "a likely winner at a losing price is not a bet"

    def test_unlikely_is_refused_even_with_edge(self):
        card = _card("DOG", model_prob=0.30, market_prob=0.22, cost=0.22)
        assert not rec.is_high_win_rate(card)


class TestFavouritesAreNotUnderrated:
    """Historical favourite win rates, 2010-2025 regular season, by closing spread."""

    @pytest.mark.parametrize("spread,actual", [(7.0, 0.743), (13.0, 0.893)])
    def test_market_margin_kernel_is_near_history(self, spread, actual):
        from app.models import distributions as D
        home, tie, _ = D.win_probability(D.market_margin_distribution(spread, 13.12))
        assert home + tie / 2 == pytest.approx(actual, abs=0.04)


class TestSportsbookConsensusTotals:
    def test_books_even_on_a_total_read_as_even_at_that_line(self):
        from app.data.odds_api import GameConsensus
        from app.models import distributions as D
        from app.strategies import book_consensus
        from tests.conftest import make_quote

        margin_profile = {3: 2.5, -3: 2.5, 7: 1.8, -7: 1.8, 43: 0.5, 44: 0.5}
        total_profile = {41: 1.2, 44: 1.2, 47: 1.2}
        row = GameConsensus(game_id="G", home="KC", away="BUF", commence="",
                            book_count=11, margin=3.0, total=43.5)
        quote = make_quote("TOTAL-44", market_type=MarketType.TOTAL, line=43.5)
        p = book_consensus.consensus_probability(
            quote, row, margin_profile, "KC", total_profile=total_profile)
        assert p == pytest.approx(0.5, abs=0.02), (
            "the margin key-number profile was being applied to game totals")

    def test_implied_centre_round_trips_through_the_margin_kernel(self):
        from app.data import odds_api
        from app.models import distributions as D
        centre = odds_api._implied_centre(-6.5, 0.70, 13.1, margin=True)
        assert D.market_margin_distribution(centre, 13.1).prob_over(-6.5) == pytest.approx(
            0.70, abs=0.01)


class TestOneContractOneEnsemble:
    def test_differently_worded_signals_on_one_ticker_combine(self):
        from app.strategies import ensemble
        a = make_signal("TOTAL-49", market_type=MarketType.TOTAL, line=48.5, label="Over 48.5",
                        model_prob=0.64, market_prob=0.655, cost=0.66)
        b = make_signal("TOTAL-49", market_type=MarketType.TOTAL, line=48.5,
                        label="Total over 48.5", model_prob=0.64, market_prob=0.655, cost=0.66)
        a.strategy, b.strategy = "totals", "book_consensus"
        assert len(ensemble.combine([a, b], multipliers={}, use_movement=False)) == 1


class TestBreakdownWording:
    def _bd(self, prob, cost):
        from app.strategies import explain
        signal = make_signal("S", model_prob=prob, market_prob=cost - 0.01, cost=cost)
        card = rec.build(signal, matchup="m", kickoff=None)
        return explain.breakdown(card, signal, None)

    def test_longshot_is_not_described_as_likely(self):
        b = self._bd(0.11, 0.10)
        text = " ".join(b["why_it_wins"] + b["how_it_loses"] + [b["verdict"]])
        assert "likely" not in text.lower().replace("unlikely", "")
        assert "1 time in 9" in text
        assert b["loses_one_in"] is None

    def test_favourite_says_how_often_it_loses(self):
        b = self._bd(0.80, 0.76)
        assert "1 time in 5" in b["how_it_loses"][0]
        assert b["verdict"].startswith(("Solid", "Likely"))


class TestPageCacheNeverBlocksOnAWarmValue:
    def test_expired_value_is_served_while_refreshing(self):
        import asyncio
        from app.core.cache import AsyncTTLCache

        async def run():
            cache = AsyncTTLCache(ttl=0.0, stale_ttl=60.0, name="t")
            cache.put("k", "old")
            calls = []

            async def slow():
                calls.append(1)
                await asyncio.sleep(0.05)
                return "new"

            first = await asyncio.wait_for(cache.get_fast("k", slow), timeout=0.01)
            await asyncio.sleep(0.1)
            return first.value, cache.peek("k").value, len(calls)

        assert asyncio.run(run()) == ("old", "new", 1)
