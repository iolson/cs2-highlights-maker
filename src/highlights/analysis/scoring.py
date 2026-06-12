"""Heuristic "watchability" score for a Moment. All knobs live in WEIGHTS."""

from __future__ import annotations

from highlights.demo.parser import DemoData
from highlights.models import Moment

WEIGHTS = {
    "kill": 1.0,
    "multikill": {2: 0.5, 3: 2.0, 4: 4.0, 5: 8.0},
    "headshot": 0.2,
    "knife": 3.0,
    "zeus": 3.0,
    "awp_noscope": 2.5,
    "thrusmoke": 1.5,
    "attackerblind": 2.0,
    "wallbang": 1.0,
    "clutch_won_base": 2.0,
    "clutch_won_per_opponent": 1.5,
    "clutch_lost_with_kills": 1.0,
    "pistol_round_multiplier": 1.15,
    "overtime_multiplier": 1.2,
    "final_round_multiplier": 1.3,
    "lost_round_multiplier": 0.85,
}

# MR12: rounds 0 and 12 are pistol rounds, overtime starts at round 24.
PISTOL_ROUND_INDEXES = (0, 12)
OVERTIME_START_INDEX = 24


def score_moment(moment: Moment, demo: DemoData) -> float:
    score = 0.0
    kill_count = moment.kill_count
    score += kill_count * WEIGHTS["kill"]
    multikill = WEIGHTS["multikill"]
    score += multikill.get(min(kill_count, 5), multikill[5] if kill_count > 5 else 0.0)

    for kill in moment.kills:
        if kill.headshot:
            score += WEIGHTS["headshot"]
        if kill.is_knife:
            score += WEIGHTS["knife"]
        if kill.is_zeus:
            score += WEIGHTS["zeus"]
        if kill.is_awp_noscope:
            score += WEIGHTS["awp_noscope"]
        if kill.thrusmoke:
            score += WEIGHTS["thrusmoke"]
        if kill.attackerblind:
            score += WEIGHTS["attackerblind"]
        if kill.penetrated > 0:
            score += WEIGHTS["wallbang"]

    if moment.clutch is not None:
        if moment.clutch.won:
            score += WEIGHTS["clutch_won_base"] + WEIGHTS["clutch_won_per_opponent"] * moment.clutch.opponents
        elif moment.clutch.kills >= 2:
            score += WEIGHTS["clutch_lost_with_kills"]

    if moment.round_index in PISTOL_ROUND_INDEXES:
        score *= WEIGHTS["pistol_round_multiplier"]
    if moment.round_index >= OVERTIME_START_INDEX:
        score *= WEIGHTS["overtime_multiplier"]
    if moment.round_index == len(demo.rounds) - 1:
        score *= WEIGHTS["final_round_multiplier"]

    round_ = demo.round_at(moment.round_index)
    if round_ is not None and moment.side and round_.winner and round_.winner != moment.side:
        score *= WEIGHTS["lost_round_multiplier"]

    return round(score, 3)


def score_moments(moments: list[Moment], demo: DemoData) -> None:
    for moment in moments:
        moment.score = score_moment(moment, demo)
