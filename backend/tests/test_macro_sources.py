"""Macro source parsing against real payloads captured 2026-11-23 (no network)."""
import json
from pathlib import Path

import pytest

from app.data.macro import fred, quotes, sentiment

FIX = Path(__file__).parent / "fixtures" / "macro"


def test_fred_csv_skips_missing_days():
    rows = fred.parse_csv("observation_date,DGS10\n2026-11-20,4.10\n2026-11-21,.\n2026-11-24,4.12\n")
    assert rows == [("2026-11-20", 4.10), ("2026-11-24", 4.12)]


def test_cpi_indicator_reports_level_mom_and_yoy_as_a_print():
    rows = fred.parse_csv((FIX / "fred_CPIAUCSL.csv").read_text())
    ind = fred.indicator("CPIAUCSL", rows)
    assert ind.kind == "print" and ind.label == "CPI" and ind.as_of == rows[-1][0]
    assert ind.value == pytest.approx(rows[-1][1])
    assert ind.mom == pytest.approx((rows[-1][1] / rows[-2][1] - 1) * 100, abs=1e-3)
    assert ind.yoy == pytest.approx((rows[-1][1] / rows[-13][1] - 1) * 100, abs=1e-3)


def test_rate_indicator_is_a_level_with_change_not_yoy():
    rows = fred.parse_csv((FIX / "fred_DGS10.csv").read_text())
    ind = fred.indicator("DGS10", rows)
    assert ind.label == "10-Year" and ind.yoy is None and ind.mom is None
    assert ind.change == pytest.approx(rows[-1][1] - rows[-2][1])


def test_yahoo_quote_and_closed_market():
    q = quotes.parse_yahoo(json.loads((FIX / "yahoo_SPY.json").read_text()), now=1_795_480_000)
    assert q.symbol == "SPY" and q.price == 722.29 and q.prev_close == 689.81
    assert q.change_pct == pytest.approx((722.29 / 689.81 - 1) * 100, abs=1e-3)
    assert q.market_open is False and q.kind == "quote"


def test_yahoo_garbage_returns_none_instead_of_raising():
    assert quotes.parse_yahoo({"chart": {"result": None, "error": {"code": "Not Found"}}}) is None


def test_coinbase_and_crypto_fear_greed():
    btc = quotes.parse_coinbase(json.loads((FIX / "coinbase_btc.json").read_text()))
    assert btc.symbol == "BTC" and btc.price > 1000
    s = sentiment.parse_alternative(json.loads((FIX / "fng.json").read_text()))
    assert s.value == 11 and s.label == "Extreme Fear" and s.as_of == 1795478400
