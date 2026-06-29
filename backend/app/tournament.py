"""
Tournament stage awareness for the 2026 FIFA World Cup.

Why this exists: a World Cup is NOT one homogeneous event. Group games are open and
high-scoring; knockout games are tight, cagey, lower-scoring, and the favourite is far
less dominant (teams play not to lose, and a single goal or a penalty shootout decides
everything). As the tournament advances, that effect gets stronger every round.

So the model and the parlay builder need to KNOW what round it is and adjust:
  - knockout football scores fewer goals          -> goals_mult  < 1
  - the favourite's edge shrinks (sides sit deep) -> supremacy_mult < 1
  - every leg is coin-flippier (one goal decides) -> combo_factor < 1 shrinks parlay confidence

Everything here is date-driven off the real 2026 schedule, so the app flips into
"knockout mode" automatically on the right day — no manual switch.
"""
from __future__ import annotations
from datetime import date
from typing import Dict, Optional

# 2026 World Cup calendar (48 teams, new Round-of-32 format).
# (stage start date, stage label, is_knockout, depth)
# depth 0 = group, then 1..5 deeper into the bracket -> tuning sharpens with depth.
_SCHEDULE = [
    (date(2026, 6, 11), "Group Stage",   False, 0),
    (date(2026, 6, 28), "Round of 32",   True,  1),
    (date(2026, 7, 4),  "Round of 16",   True,  2),
    (date(2026, 7, 9),  "Quarter-finals", True, 3),
    (date(2026, 7, 14), "Semi-finals",   True,  4),
    (date(2026, 7, 18), "Final / 3rd Place", True, 5),
]
_TOURNAMENT_END = date(2026, 7, 20)

# Per-depth tuning. Group stage is the neutral baseline (all 1.0). Knockout rounds get
# progressively tighter, lower-scoring, and coin-flippier the deeper the bracket goes.
#   goals_mult     : scales the model's baseline total goals (knockouts are lower-scoring)
#   supremacy_mult : scales the favourite's goal supremacy (favourites are less dominant)
#   combo_factor   : multiplies a parlay's combined probability (single goals decide ties)
_DEPTH_TUNING = {
    0: {"goals_mult": 1.00, "supremacy_mult": 1.00, "combo_factor": 1.00},
    1: {"goals_mult": 0.93, "supremacy_mult": 0.88, "combo_factor": 0.97},  # R32
    2: {"goals_mult": 0.90, "supremacy_mult": 0.84, "combo_factor": 0.95},  # R16
    3: {"goals_mult": 0.88, "supremacy_mult": 0.80, "combo_factor": 0.93},  # QF
    4: {"goals_mult": 0.86, "supremacy_mult": 0.77, "combo_factor": 0.91},  # SF
    5: {"goals_mult": 0.84, "supremacy_mult": 0.74, "combo_factor": 0.90},  # Final
}


def current_stage(today: Optional[date] = None) -> Dict:
    """
    Return the active tournament stage + its tuning factors for a given date
    (defaults to today). Outside the tournament window it returns the neutral
    group-stage baseline so nothing breaks off-season.
    """
    today = today or date.today()
    stage_label, is_knockout, depth = "Group Stage", False, 0
    for start, label, knockout, d in _SCHEDULE:
        if today >= start:
            stage_label, is_knockout, depth = label, knockout, d
        else:
            break
    # Past the final -> fall back to neutral so the model behaves normally.
    if today >= _TOURNAMENT_END:
        stage_label, is_knockout, depth = "Off-season", False, 0

    tuning = _DEPTH_TUNING[depth]
    return {
        "stage": stage_label,
        "is_knockout": is_knockout,
        "depth": depth,
        "label": (f"🏆 {stage_label} — knockout mode" if is_knockout else stage_label),
        **tuning,
    }
