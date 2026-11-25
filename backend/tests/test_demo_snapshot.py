"""Demo mode is the public face of a private terminal: nothing account-shaped may ship in it."""
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("snapshot_demo", ROOT / "backend/scripts/snapshot_demo.py")
snapshot_demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot_demo)

FORBIDDEN = re.compile(r"(?i)balance|bankroll|position|account|user|email|order_id|client_order|kalshi_key")


def test_sanitize_drops_account_fields_at_any_depth():
    raw = {"brier": 0.09, "games": 162, "bankroll": 1000,
           "nested": {"account_id": "A1", "positions": [1], "keep": 1},
           "rows": [{"user": "ashar", "edge": 0.02}]}
    clean = snapshot_demo.sanitize(raw)
    assert clean == {"brier": 0.09, "games": 162, "nested": {"keep": 1}, "rows": [{"edge": 0.02}]}


def test_me_snapshot_is_the_fixed_demo_identity():
    assert snapshot_demo.demo_me() == {"user": "demo",
                                       "live_trading": {"allowed": False, "reason": "demo"}}


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_committed_snapshots_carry_no_account_fields():
    files = sorted((ROOT / "frontend/public/demo").glob("*.json"))
    assert files, "demo snapshot missing — run backend/scripts/snapshot_demo.py"
    for f in files:
        data = json.loads(f.read_text())
        bad = [k for k in _keys(data) if FORBIDDEN.search(k)]
        if f.name == "me.json":
            bad = [k for k in bad if k != "user"]   # the fixed "demo" identity
        assert not bad, f"{f.name} carries {bad}"


def test_demo_board_never_offers_in_app_trading():
    board = {"cards": [{"id": "c1", "trade": {"url": "https://kalshi.com/markets/x", "in_app": True}}]}
    assert snapshot_demo.demo_board(board)["cards"][0]["trade"] == {"url": "https://kalshi.com/markets/x", "in_app": False}


def test_snapshot_covers_the_module_2_pages():
    for path in ("/board", "/research/evidence", "/markets/browse", "/research/briefs"):
        assert path in snapshot_demo.ENDPOINTS


def test_snapshot_covers_every_sports_league():
    from app.data.sports.leagues import LEAGUES
    assert "/sports" in snapshot_demo.ENDPOINTS
    for key in LEAGUES:
        assert f"/sports/{key}" in snapshot_demo.ENDPOINTS
        assert (ROOT / "frontend/public/demo" / f"sports_{key}.json").exists()
