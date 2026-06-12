"""Cluster kills into Moments: bursts of action by one player in one round."""

from __future__ import annotations

from collections import defaultdict

from highlights.models import Kill, Moment


def cluster_moments(kills: list[Kill], tickrate: float, gap_seconds: float = 20.0) -> list[Moment]:
    max_gap_ticks = int(gap_seconds * tickrate)
    by_player_round: dict[tuple[str, int], list[Kill]] = defaultdict(list)
    for kill in kills:
        by_player_round[(kill.attacker_steamid, kill.round_index)].append(kill)

    moments: list[Moment] = []
    for (steamid, round_index), player_kills in by_player_round.items():
        player_kills.sort(key=lambda k: k.tick)
        current: list[Kill] = []
        for kill in player_kills:
            if current and kill.tick - current[-1].tick > max_gap_ticks:
                moments.append(_make_moment(steamid, round_index, current))
                current = []
            current.append(kill)
        if current:
            moments.append(_make_moment(steamid, round_index, current))

    moments.sort(key=lambda m: m.first_tick)
    return moments


def _make_moment(steamid: str, round_index: int, kills: list[Kill]) -> Moment:
    return Moment(
        steamid=steamid,
        name=kills[0].attacker_name,
        side=kills[0].attacker_side,
        round_index=round_index,
        kills=list(kills),
    )
