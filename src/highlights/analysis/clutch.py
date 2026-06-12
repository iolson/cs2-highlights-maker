"""Detect 1vX clutch situations by replaying deaths against round rosters."""

from __future__ import annotations

from collections import defaultdict

from highlights.demo.parser import DemoData
from highlights.models import Clutch, Moment


def detect_clutches(demo: DemoData) -> list[Clutch]:
    deaths_by_round: dict[int, list] = defaultdict(list)
    for death in demo.deaths:
        deaths_by_round[death.round_index].append(death)

    kills_by_round: dict[int, list] = defaultdict(list)
    for kill in demo.kills:
        kills_by_round[kill.round_index].append(kill)

    clutches: list[Clutch] = []
    for round_ in demo.rounds:
        roster = demo.rosters.get(round_.index) or {}
        if not roster:
            continue
        alive: dict[str, set[str]] = {"CT": set(), "T": set()}
        for steamid, side in roster.items():
            alive[side].add(steamid)
        if not alive["CT"] or not alive["T"]:
            continue

        clutch_started: set[str] = set()  # sides that already entered a clutch this round
        for death in sorted(deaths_by_round.get(round_.index, []), key=lambda d: d.tick):
            for side in ("CT", "T"):
                alive[side].discard(death.victim_steamid)
            for side, enemy_side in (("CT", "T"), ("T", "CT")):
                if side in clutch_started:
                    continue
                if len(alive[side]) == 1 and len(alive[enemy_side]) >= 1:
                    clutch_started.add(side)
                    clutcher = next(iter(alive[side]))
                    opponents = len(alive[enemy_side])
                    kills_in_clutch = sum(
                        1
                        for k in kills_by_round.get(round_.index, [])
                        if k.attacker_steamid == clutcher and k.tick >= death.tick
                    )
                    clutches.append(
                        Clutch(
                            steamid=clutcher,
                            name=demo.player_names.get(clutcher, ""),
                            side=side,
                            opponents=opponents,
                            won=round_.winner == side,
                            kills=kills_in_clutch,
                            start_tick=death.tick,
                            end_tick=round_.end_tick,
                            round_index=round_.index,
                        )
                    )
    return clutches


def attach_clutches(moments: list[Moment], clutches: list[Clutch], demo: DemoData) -> list[Moment]:
    """Attach each clutch to the clutcher's moment in that round (or synthesize one)."""
    by_player_round: dict[tuple[str, int], list[Moment]] = defaultdict(list)
    for moment in moments:
        by_player_round[(moment.steamid, moment.round_index)].append(moment)

    result = list(moments)
    for clutch in clutches:
        if not clutch.won and clutch.kills < 2:
            continue  # not worth watching
        candidates = by_player_round.get((clutch.steamid, clutch.round_index), [])
        # Prefer the moment whose action overlaps the clutch window.
        target = None
        for moment in candidates:
            if moment.kills and moment.kills[-1].tick >= clutch.start_tick:
                target = moment
                break
        if target is None and candidates:
            target = candidates[-1]
        if target is not None:
            target.clutch = clutch
        elif clutch.won:
            # Clutch won without kills after the situation started (e.g. a defuse).
            result.append(
                Moment(
                    steamid=clutch.steamid,
                    name=clutch.name,
                    side=clutch.side,
                    round_index=clutch.round_index,
                    kills=[],
                    clutch=clutch,
                )
            )
    result.sort(key=lambda m: m.first_tick)
    return result
