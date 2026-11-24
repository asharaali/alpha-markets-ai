"""Fed decision odds from Kalshi's KXFED ladder, and recession odds (payloads from 2026-11-23).

KXFED rules: "If the upper bound of the target federal funds rate ... is greater than X% following
the meeting, the market resolves to Yes." So P(upper > X) is a ladder, and outcome odds relative to
today's upper bound are differences between rungs."""
import json
from pathlib import Path

import pytest

from app.data.macro import kalshi_macro

FIX = Path(__file__).parent / "fixtures" / "macro"
FED = json.loads((FIX / "kalshi_fed.json").read_text())


def test_next_meeting_is_chosen_and_outcomes_are_relative_to_todays_target():
    odds = kalshi_macro.fed_decision(FED, current_upper=4.00, now=1_795_400_000)
    assert odds.event == "KXFED-26DEC" and odds.meeting_date.startswith("2026-12-09")
    by = {o["label"]: o["prob"] for o in odds.outcomes}
    assert by["Hike 25bp"] == pytest.approx(0.965 - 0.045, abs=1e-3)
    assert by["Hold"] == pytest.approx(0.995 - 0.965, abs=1e-3)
    assert sum(by.values()) == pytest.approx(1.0, abs=1e-6)
    assert odds.outcomes[0]["label"] == "Hike 25bp"          # most likely first
    assert odds.kind == "market" and odds.url == "https://kalshi.com/markets/kxfed"


def test_a_non_monotone_ladder_is_repaired_not_turned_into_negative_odds():
    jan = {"events": [FED["events"][1]]}                     # 3.75 mid 0.95 < 4.00 mid 0.95 / noisy
    odds = kalshi_macro.fed_decision(jan, current_upper=4.00, now=1_795_400_000)
    assert all(o["prob"] >= 0 for o in odds.outcomes)
    assert sum(o["prob"] for o in odds.outcomes) == pytest.approx(1.0, abs=1e-6)


def test_recession_odds_use_the_mid():
    rec = kalshi_macro.recession(json.loads((FIX / "kalshi_recession.json").read_text()))
    assert rec.prob == pytest.approx(0.025) and rec.kind == "market"
    assert rec.label == "Recession this year?"
