from conftest import make_demo, make_kill, make_round

from highlights.analysis.clutch import attach_clutches, detect_clutches
from highlights.analysis.moments import cluster_moments
from highlights.demo.parser import Death


def test_won_1v2_clutch_detected():
    # CTs: a1, a2. Ts: b1, b2, b3. a2 dies -> a1 is 1v3... use 2v3 for clarity.
    rosters = {0: {"a1": "CT", "a2": "CT", "b1": "T", "b2": "T"}}
    rounds = [make_round(0, 0, 10000, winner="CT")]
    # a2 dies at 2000 -> a1 in 1v2; a1 kills b1 (3000) and b2 (4000).
    kills = [
        make_kill(2000, attacker="b1", victim="a2", attacker_side="T"),
        make_kill(3000, attacker="a1", victim="b1", attacker_side="CT"),
        make_kill(4000, attacker="a1", victim="b2", attacker_side="CT"),
    ]
    deaths = [Death(k.tick, 0, k.victim_steamid) for k in kills]
    demo = make_demo(kills, rounds, deaths=deaths, rosters=rosters)

    clutches = detect_clutches(demo)
    won = [c for c in clutches if c.won]
    assert len(won) == 1
    clutch = won[0]
    assert clutch.steamid == "a1"
    assert clutch.opponents == 2
    assert clutch.kills == 2
    assert clutch.start_tick == 2000


def test_lost_clutch_for_other_side():
    rosters = {0: {"a1": "CT", "a2": "CT", "b1": "T", "b2": "T"}}
    rounds = [make_round(0, 0, 10000, winner="CT")]
    kills = [
        make_kill(2000, attacker="b1", victim="a2", attacker_side="T"),
        make_kill(3000, attacker="a1", victim="b1", attacker_side="CT"),
        make_kill(4000, attacker="a1", victim="b2", attacker_side="CT"),
    ]
    deaths = [Death(k.tick, 0, k.victim_steamid) for k in kills]
    demo = make_demo(kills, rounds, deaths=deaths, rosters=rosters)

    clutches = detect_clutches(demo)
    lost = [c for c in clutches if not c.won]
    # b2 ends up alone (1v1) after b1 dies at 3000 and loses.
    assert len(lost) == 1
    assert lost[0].steamid == "b2"
    assert lost[0].opponents == 1


def test_attach_clutch_to_moment(tickrate):
    rosters = {0: {"a1": "CT", "a2": "CT", "b1": "T", "b2": "T"}}
    rounds = [make_round(0, 0, 10000, winner="CT")]
    kills = [
        make_kill(2000, attacker="b1", victim="a2", attacker_side="T"),
        make_kill(3000, attacker="a1", victim="b1", attacker_side="CT"),
        make_kill(4000, attacker="a1", victim="b2", attacker_side="CT"),
    ]
    deaths = [Death(k.tick, 0, k.victim_steamid) for k in kills]
    demo = make_demo(kills, rounds, deaths=deaths, rosters=rosters)

    moments = cluster_moments(demo.kills, tickrate)
    clutches = detect_clutches(demo)
    moments = attach_clutches(moments, clutches, demo)

    a1_moments = [m for m in moments if m.steamid == "a1"]
    assert len(a1_moments) == 1
    assert a1_moments[0].clutch is not None
    assert a1_moments[0].clutch.won
    # Won clutch extends the moment to the round end.
    assert a1_moments[0].last_tick == 10000
