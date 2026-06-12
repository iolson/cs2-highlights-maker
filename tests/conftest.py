from pathlib import Path

import pytest

from highlights.demo.parser import DemoData, Death
from highlights.models import Kill, Round

TICKRATE = 64.0


def make_kill(
    tick: int,
    round_index: int = 0,
    attacker: str = "111",
    victim: str = "222",
    attacker_side: str = "CT",
    weapon: str = "ak47",
    **flags,
) -> Kill:
    return Kill(
        tick=tick,
        round_index=round_index,
        attacker_steamid=attacker,
        attacker_name=f"player{attacker}",
        attacker_side=attacker_side,
        victim_steamid=victim,
        victim_name=f"player{victim}",
        weapon=weapon,
        **flags,
    )


def make_round(index: int, start: int, end: int, winner: str = "CT") -> Round:
    return Round(
        index=index,
        start_tick=start,
        freeze_end_tick=start + 1000,
        end_tick=end,
        winner=winner,
    )


def make_demo(
    kills: list[Kill],
    rounds: list[Round],
    deaths: list[Death] | None = None,
    rosters: dict | None = None,
    slots: dict | None = None,
) -> DemoData:
    names = {}
    for kill in kills:
        names.setdefault(kill.attacker_steamid, kill.attacker_name)
        names.setdefault(kill.victim_steamid, kill.victim_name)
    return DemoData(
        path=Path("test.dem"),
        map_name="de_test",
        tickrate=TICKRATE,
        kills=kills,
        deaths=deaths
        if deaths is not None
        else [Death(tick=k.tick, round_index=k.round_index, victim_steamid=k.victim_steamid) for k in kills],
        rounds=rounds,
        player_names=names,
        slots=slots if slots is not None else {sid: i + 1 for i, sid in enumerate(sorted(names))},
        rosters=rosters or {},
        match_start_tick=0,
        last_tick=rounds[-1].end_tick + 640 if rounds else 100000,
    )


@pytest.fixture
def tickrate() -> float:
    return TICKRATE
