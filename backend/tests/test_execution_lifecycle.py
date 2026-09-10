"""Execution: fills, fees, partial closes, reconciliation and the edge re-check.

These tests exercise the paths where the app touches money. Every one of them describes a
way the previous implementation could report a position the user did not hold, at a price
they did not pay, with an edge that no longer existed.

No network. The Kalshi book and order endpoint are stubbed, which is the point: the
behaviour under a partial fill or a timeout has to be verifiable without waiting for one to
happen in production.
"""
from __future__ import annotations

import time

import pytest

from app.core.types import Side
from app.data.kalshi import execution
from app.risk import fees
from app.tracking import store


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets its own database file so nothing leaks between them.

    The stake ceilings are raised here because this machine's .env sets them to the small
    real-money limits the owner trades with. The tests are about arithmetic, not about the
    ceiling, and a $5 cap would make every fill one contract.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "HARD_MAX_STAKE", 10_000.0)
    monkeypatch.setattr(settings, "HARD_DAILY_CAP", 100_000.0)
    store._initialised = False
    if hasattr(store._local, "conn"):
        store._local.conn.close()
        del store._local.conn
    store.init()
    yield
    store._initialised = False
    if hasattr(store._local, "conn"):
        store._local.conn.close()
        del store._local.conn


def stub_book(monkeypatch, *, cost, depth):
    async def _read_price(ticker, side):
        return cost, depth
    monkeypatch.setattr(execution, "read_price", _read_price)


class TestEdgeRecheckAtSubmission:
    """The price the user clicked is not necessarily the price they get."""

    def test_price_past_the_maximum_entry_is_refused(self):
        ok, why = execution.edge_still_available(
            live_cost=0.62, model_prob=0.70, recommended_cost=0.55,
            max_entry_price=0.58, contracts=100)
        assert ok is False
        assert "edge no longer available" in why

    def test_price_inside_tolerance_is_allowed(self):
        ok, _ = execution.edge_still_available(
            live_cost=0.56, model_prob=0.70, recommended_cost=0.55,
            max_entry_price=0.60, contracts=100)
        assert ok is True

    def test_edge_erased_by_fees_is_refused_even_within_tolerance(self):
        """The check the old code never made.

        A one-cent move can leave the price inside every tolerance and still take the bet
        below break-even once the fee is charged.
        """
        ok, why = execution.edge_still_available(
            live_cost=0.509, model_prob=0.51, recommended_cost=0.50,
            max_entry_price=0.60, contracts=100)
        assert ok is False
        assert "break even after fees" in why

    def test_no_model_probability_means_only_price_gates_apply(self):
        ok, _ = execution.edge_still_available(
            live_cost=0.56, model_prob=None, recommended_cost=0.55,
            max_entry_price=None, contracts=100)
        assert ok is True

    @pytest.mark.asyncio
    async def test_place_refuses_when_the_book_has_moved(self, monkeypatch):
        stub_book(monkeypatch, cost=0.68, depth=50000.0)
        fill = await execution.place(
            username="t", ticker="NFL-KC", side="yes", stake=100.0,
            model_prob=0.70, recommended_cost=0.55, max_entry_price=0.58)
        assert fill.ok is False
        assert fill.reason == "edge_gone"
        assert fill.contracts == 0
        assert store.positions_for("t") == [], "a refused order must not leave a position"


class TestPaperFillsRespectTheBook:
    @pytest.mark.asyncio
    async def test_paper_fill_is_capped_by_resting_depth(self, monkeypatch):
        # $100 at 50c wants 200 contracts, but only $40 is resting: 80 contracts.
        stub_book(monkeypatch, cost=0.50, depth=40.0)
        fill = await execution.place(username="t", ticker="NFL-KC", side="yes",
                                     stake=100.0, skip_edge_check=True)
        assert fill.ok is True
        assert fill.status == "partially_filled"
        assert fill.contracts == 80
        assert fill.requested_contracts == 200
        assert fill.remaining_contracts == 120

    @pytest.mark.asyncio
    async def test_no_depth_means_no_fill_and_no_position(self, monkeypatch):
        stub_book(monkeypatch, cost=0.50, depth=0.10)
        fill = await execution.place(username="t", ticker="NFL-KC", side="yes",
                                     stake=100.0, skip_edge_check=True)
        assert fill.ok is False
        assert fill.status == "unfilled"
        assert store.positions_for("t") == []

    @pytest.mark.asyncio
    async def test_paper_fill_charges_the_fee(self, monkeypatch):
        stub_book(monkeypatch, cost=0.50, depth=50000.0)
        fill = await execution.place(username="t", ticker="NFL-KC", side="yes",
                                     stake=100.0, skip_edge_check=True)
        assert fill.contracts == 200
        assert fill.fees == pytest.approx(
            fees.trading_fee(contracts=200, price=0.50), abs=0.001)
        assert fill.fees > 0


class TestLiveOrderResponseParsing:
    """An HTTP 200 is not a fill."""

    def test_accepted_but_unfilled_order_reports_zero(self):
        result = execution.parse_order_response(
            {"order": {"order_id": "abc", "status": "canceled",
                       "taker_fill_count": 0, "remaining_count": 0}},
            side=Side.YES, requested=100, fallback_price=0.55)
        assert result.ok is False
        assert result.filled == 0

    def test_partial_fill_reports_the_filled_count_only(self):
        result = execution.parse_order_response(
            {"order": {"order_id": "abc", "status": "executed",
                       "taker_fill_count": 37, "remaining_count": 63,
                       "taker_fill_cost": 37 * 5500, "taker_fees": 35}},
            side=Side.YES, requested=100, fallback_price=0.55)
        assert result.ok is True
        assert result.filled == 37
        assert result.remaining == 63
        assert result.avg_price == pytest.approx(0.55, abs=1e-6)

    def test_average_price_comes_from_the_fill_cost_not_the_request(self):
        """Filled at 58c after asking for 55c: the position must record 58c."""
        result = execution.parse_order_response(
            {"order": {"order_id": "abc", "status": "executed",
                       "taker_fill_count": 100, "remaining_count": 0,
                       "taker_fill_cost": 100 * 5800}},
            side=Side.YES, requested=100, fallback_price=0.55)
        assert result.avg_price == pytest.approx(0.58, abs=1e-6)

    def test_missing_fill_data_falls_back_without_inventing_a_fill(self):
        result = execution.parse_order_response(
            {"order": {"order_id": "abc", "status": "executed"}},
            side=Side.YES, requested=100, fallback_price=0.55)
        assert result.filled == 0, "no reported fills must not become a full fill"


class TestPositionAccountingWithFees:
    def test_pnl_is_net_of_entry_and_exit_fees(self):
        entry_fee = fees.trading_fee(contracts=100, price=0.50)
        position_id = store.open_position(
            user="t", mode="paper", ticker="NFL-KC", side="yes", contracts=100,
            entry_price=0.50, stake=50.0, filled_contracts=100, entry_fees=entry_fee,
            status="open")
        exit_fee = fees.trading_fee(contracts=100, price=0.60)
        closed = store.close_position(position_id, exit_price=0.60, exit_fee=exit_fee)
        gross = (0.60 - 0.50) * 100
        assert closed["pnl"] == pytest.approx(gross - entry_fee - exit_fee, abs=0.001)
        assert closed["pnl"] < gross

    def test_partial_close_leaves_the_remainder_open(self):
        position_id = store.open_position(
            user="t", mode="paper", ticker="NFL-KC", side="yes", contracts=100,
            entry_price=0.50, stake=50.0, filled_contracts=100, entry_fees=0.0,
            status="open")
        first = store.close_position(position_id, exit_price=0.60, contracts=40)
        assert first["status"] == "partially_closed"
        assert first["remaining_open"] == 60
        assert first["contracts_closed"] == 40

        rows = store.positions_for("t")
        assert rows[0]["status"] == "partially_closed"
        assert rows[0]["closed_contracts"] == 40

        second = store.close_position(position_id, exit_price=0.70)
        assert second["status"] == "closed"
        assert second["contracts_closed"] == 60

    def test_closing_more_than_is_held_closes_only_what_is_held(self):
        position_id = store.open_position(
            user="t", mode="paper", ticker="NFL-KC", side="yes", contracts=50,
            entry_price=0.50, stake=25.0, filled_contracts=50, status="open")
        closed = store.close_position(position_id, exit_price=0.60, contracts=500)
        assert closed["contracts_closed"] == 50
        assert closed["status"] == "closed"

    def test_settlement_pays_a_dollar_per_winning_contract(self):
        entry_fee = fees.trading_fee(contracts=100, price=0.40)
        position_id = store.open_position(
            user="t", mode="paper", ticker="NFL-KC", side="yes", contracts=100,
            entry_price=0.40, stake=40.0, filled_contracts=100, entry_fees=entry_fee,
            status="open")
        settled = store.settle_position(position_id, won=True)
        assert settled["status"] == "settled"
        assert settled["pnl"] == pytest.approx((1.0 - 0.40) * 100 - entry_fee, abs=0.001)

    def test_settlement_of_a_loser_costs_the_stake_plus_the_fee(self):
        entry_fee = fees.trading_fee(contracts=100, price=0.40)
        position_id = store.open_position(
            user="t", mode="paper", ticker="NFL-KC", side="yes", contracts=100,
            entry_price=0.40, stake=40.0, filled_contracts=100, entry_fees=entry_fee,
            status="open")
        settled = store.settle_position(position_id, won=False)
        assert settled["pnl"] == pytest.approx(-40.0 - entry_fee, abs=0.001)

    def test_a_settled_position_cannot_be_settled_twice(self):
        position_id = store.open_position(
            user="t", mode="paper", ticker="NFL-KC", side="yes", contracts=10,
            entry_price=0.40, stake=4.0, filled_contracts=10, status="open")
        assert store.settle_position(position_id, won=True) is not None
        assert store.settle_position(position_id, won=True) is None


class TestPaperAndLiveStayApart:
    def test_daily_cap_counts_each_mode_separately(self):
        store.open_position(user="t", mode="paper", ticker="A", side="yes",
                            contracts=10, entry_price=0.5, stake=200.0,
                            filled_contracts=10, status="open")
        recent = time.time() - 60
        assert store.staked_since("t", since=recent, mode="paper") == pytest.approx(200.0)
        assert store.staked_since("t", since=recent, mode="live") == pytest.approx(0.0)

    def test_paper_exposure_does_not_block_a_live_order(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "HARD_DAILY_CAP", 250.0)
        store.open_position(user="t", mode="paper", ticker="A", side="yes",
                            contracts=10, entry_price=0.5, stake=200.0,
                            filled_contracts=10, status="open")
        # Live is untouched, so a $100 live order is fine even though paper is near the cap.
        execution.check_limits(100.0, "t", "live")
        # The same order on paper would breach it.
        with pytest.raises(Exception):
            execution.check_limits(100.0, "t", "paper")


class TestSettlementUsesStoredMetadata:
    @pytest.mark.asyncio
    async def test_no_side_position_settles_on_the_inverted_outcome(self):
        from app.tracking import grading

        outcome = grading.resolve_contract(
            market_type="moneyline", selection="KC ML", team="KC", line=None,
            home="KC", home_score=17, away_score=24, side="no")
        # KC lost, so a NO contract on KC pays.
        assert outcome is True

    def test_position_keeps_the_metadata_needed_to_settle(self):
        position_id = store.open_position(
            user="t", mode="paper", ticker="NFL-KC-SPREAD", side="yes", contracts=10,
            entry_price=0.5, stake=5.0, market_type="spread", team="KC", line=3.5,
            selection="KC -3.5", filled_contracts=10, status="open")
        row = [p for p in store.positions_for("t") if p["id"] == position_id][0]
        assert row["team"] == "KC"
        assert row["line"] == 3.5
        assert row["selection"] == "KC -3.5"
