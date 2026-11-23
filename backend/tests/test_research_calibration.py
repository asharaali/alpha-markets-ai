"""Calibrated hit %: the board shows what a family's calls have actually hit, not what the
model believes, and leans on the market price where its own history is thin."""
import pytest

from app.research.calibration import SHRINK_K, Calibrator


def _row(i, p, hit, market=0.5):
    return {"game_id": f"g{i}", "strategy": "ensemble", "market_type": "spread", "selection": f"s{i}",
            "created_at": float(i), "outcome": int(hit), "model_prob": p, "fair_prob": p, "market_prob": market}


def test_overconfident_long_shots_are_pulled_down():
    rows = [_row(i, 0.15, i < 15) for i in range(300)]       # says 15%, hits 5%
    cal = Calibrator().fit(rows)
    assert SHRINK_K == 30
    assert cal.apply(0.15, 0.10) == pytest.approx((300 * 0.05 + 30 * 0.10) / 330)


def test_empty_bin_returns_market_price():
    cal = Calibrator().fit([_row(i, 0.15, i < 15) for i in range(300)])
    assert cal.apply(0.85, 0.80) == pytest.approx(0.80)


def test_no_market_price_anchors_to_raw():
    assert Calibrator().fit([]).apply(0.4, None) == pytest.approx(0.4)


def test_refreshes_are_collapsed_before_counting():
    rows = [dict(_row(0, 0.15, False), created_at=float(t)) for t in range(500)]
    cal = Calibrator().fit(rows)
    assert cal.table() == [{"bucket": "10-20%", "n": 1, "predicted": 0.15, "observed": 0.0}]


def test_table_reports_each_populated_bucket():
    rows = [_row(i, 0.15, i < 15) for i in range(300)] + [_row(1000 + i, 0.72, i < 70) for i in range(100)]
    table = Calibrator().fit(rows).table()
    assert [t["bucket"] for t in table] == ["10-20%", "70-80%"]
    assert table[1] == {"bucket": "70-80%", "n": 100, "predicted": 0.72, "observed": 0.7}
