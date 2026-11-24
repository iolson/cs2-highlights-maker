"""Shared fixtures for the broadcast-sync tests: a small demo, real reports, anchor files."""

import json
from pathlib import Path

from conftest import make_demo, make_kill, make_round

from highlights.broadcast.models import Anchor, AnchorFile, Segment
from highlights.models import CameraAction, Moment, Sequence
from highlights.report import write_report

TR = 64.0
POV = "111"
# Rounds (ticks): 1: 1000-6000, 2: 7000-12000, 3: 13000-18000, 4: 19000-24000 (freeze ends 1000 ticks in).
# Round 2 freeze ends at tick 8000.
ROUND2_FREEZE_END = 8000


def make_test_demo(name="final.dem", map_name="de_test"):
    rounds = [make_round(i, 1000 + i * 6000, 6000 + i * 6000) for i in range(4)]
    kills = [
        make_kill(8672, round_index=1, attacker=POV, victim="222"),
        make_kill(9616, round_index=1, attacker=POV, victim="333"),
        make_kill(15000, round_index=2, attacker=POV, victim="222"),
        make_kill(15400, round_index=2, attacker=POV, victim="333"),
    ]
    demo = make_demo(kills, rounds)
    demo.path = Path(name)
    demo.map_name = map_name
    return demo


def make_sequence(demo, number, round_index, start, end, **extra):
    kills = [k for k in demo.kills if k.round_index == round_index and start <= k.tick <= end]
    moment = Moment(steamid=POV, name=f"player{POV}", side="CT", round_index=round_index, kills=kills)
    return Sequence(
        number=number,
        start_tick=start,
        end_tick=end,
        cameras=[CameraAction(start, POV)],
        label="test",
        score=1.0,
        round_index=round_index,
        moments=[moment] if kills else [],
        **extra,
    )


def write_test_report(path, demo, sequences):
    write_report(path, demo, sequences, spatial_by_sequence={})
    return path


def anchor_file(path, segments, demo="final.dem", vod=None):
    return AnchorFile(path=path, match_id=None, demo=demo, vod=vod, segments=segments)


def segment(seg_id="main", anchors=((ROUND2_FREEZE_END, 10.0),), rounds=(None, None)):
    return Segment(
        seg_id,
        "live",
        rounds[0],
        rounds[1],
        tuple(Anchor(tick, vod, "freeze_end") for tick, vod in anchors),
    )


def write_anchor_json(path, segments, demo="final.dem", vod=None):
    data = {
        "schema_version": 1,
        "demo": demo,
        "segments": [
            {
                "id": seg_id,
                "kind": "live",
                **({"round_start": rounds[0], "round_end": rounds[1]} if rounds else {}),
                "anchors": [{"demo_tick": t, "vod_seconds": v, "verified": True} for t, v in anchors],
            }
            for seg_id, rounds, anchors in segments
        ],
    }
    if vod:
        data["vod"] = vod
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    return path
