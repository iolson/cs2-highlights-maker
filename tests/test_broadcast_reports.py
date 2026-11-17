import json
from pathlib import Path

import pytest
from broadcast_helpers import (
    ROUND2_FREEZE_END,
    anchor_file,
    make_sequence,
    make_test_demo,
    segment,
    write_test_report,
)

from highlights.broadcast.matching import plan_report
from highlights.broadcast.models import BroadcastSyncError
from highlights.broadcast.reports import check_against_demo, load_report, not_a_highlight_report
from highlights.broadcast.timeline import Timeline


@pytest.fixture
def demo():
    return make_test_demo()


def _report(tmp_path, demo, sequences, name="out.json"):
    return load_report(write_test_report(tmp_path / name, demo, sequences))


def _plan(demo, report, segments, mode="highlight", lead_in=3.0, lead_out=4.0, duration=500.0, **kw):
    timeline = Timeline(demo, anchor_file(Path("a.json"), segments))
    return plan_report(timeline, report, mode, lead_in, lead_out, duration, **kw)


# --- report loading -----------------------------------------------------------------


def test_run_report_with_several_sequences(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 1, 8400, 9700), make_sequence(demo, 2, 2, 14800, 15600)])
    assert [(s.number, s.round) for s in report.sequences] == [(1, 2), (2, 3)]
    assert report.sequences[0].kill_ticks == [8672, 9616]
    assert report.demo_name == "final.dem"
    assert report.clip_path is None  # --json-only: no mp4 beside the report


def test_cinematic_takes_are_skipped(tmp_path, demo):
    pov = make_sequence(demo, 1, 1, 8400, 9700)
    take = make_sequence(demo, 2, 1, 8400, 9700, campath=Path("x.campath"), cinematic_of=1)
    report = _report(tmp_path, demo, [pov, take])
    assert [s.number for s in report.sequences] == [1]


def test_batch_clip_finds_its_mp4_and_measured_length(tmp_path, demo):
    path = write_test_report(tmp_path / "lan-p-1.json", demo, [make_sequence(demo, 1, 1, 8400, 9700)])
    (tmp_path / "lan-p-1.mp4").write_bytes(b"")
    data = json.loads(path.read_text())
    data["sequences"][0]["clip_duration_seconds"] = 21.5
    path.write_text(json.dumps(data))
    report = load_report(path)
    assert report.clip_path == tmp_path / "lan-p-1.mp4"
    assert report.sequences[0].clip_duration_seconds == 21.5


def test_reports_that_are_not_highlight_reports(tmp_path):
    assert not_a_highlight_report({"kind": "broll", "sequences": []})
    assert not_a_highlight_report({"foo": 1})
    assert not_a_highlight_report([])
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"demo": "x.dem", "sequences": [], "schema_version": 2}))
    with pytest.raises(BroadcastSyncError, match="schema_version"):
        load_report(path)
    with pytest.raises(BroadcastSyncError, match="not found"):
        load_report(tmp_path / "nope.json")


def test_sequence_missing_a_field_fails_clearly(tmp_path, demo):
    path = write_test_report(tmp_path / "r.json", demo, [make_sequence(demo, 1, 1, 8400, 9700)])
    data = json.loads(path.read_text())
    del data["sequences"][0]["start_tick"]
    path.write_text(json.dumps(data))
    with pytest.raises(BroadcastSyncError, match="sequence 1"):
        load_report(path)


# --- demo identity -------------------------------------------------------------------


def test_report_from_another_demo_is_rejected(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 1, 8400, 9700)])
    with pytest.raises(BroadcastSyncError, match="not 'other.dem'"):
        check_against_demo(report, demo, Path("other.dem"))


def test_same_round_number_on_another_map_is_not_confused(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 0, 1200, 2400)])
    other = make_test_demo(map_name="de_other")
    with pytest.raises(BroadcastSyncError, match="map 'de_test' but the demo is 'de_other'"):
        check_against_demo(report, other, Path("final.dem"))


def test_incompatible_reports_fail(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 1, 8400, 9700)])
    check_against_demo(report, demo, Path("C:/demos/FINAL.dem"))  # name match ignores case and folders

    wrong_rounds = make_test_demo()
    wrong_rounds.rounds = wrong_rounds.rounds[:3]
    with pytest.raises(BroadcastSyncError, match="4 rounds but the demo has 3"):
        check_against_demo(report, wrong_rounds, Path("final.dem"))

    short = make_test_demo()
    short.last_tick = 9000
    with pytest.raises(BroadcastSyncError, match="after the demo's last tick"):
        check_against_demo(report, short, Path("final.dem"))

    report.sequences[0].round = 9
    with pytest.raises(BroadcastSyncError, match="round 9 does not exist"):
        check_against_demo(report, demo, Path("final.dem"))
    report.sequences[0].round = 2
    report.sequences[0].kill_ticks = [100]
    with pytest.raises(BroadcastSyncError, match="outside the clip"):
        check_against_demo(report, demo, Path("final.dem"))


# --- planning ------------------------------------------------------------------------


def test_matched_when_an_anchor_is_in_the_same_round(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 1, 8400, 9700)])
    (plan,) = _plan(demo, report, [segment()])
    assert plan.status == "matched"
    assert "anchor_in_same_round" in plan.basis and "single_anchor" in plan.basis
    assert plan.window.vod_start == pytest.approx(10.0 + (8400 - 3 * 64 - 8000) / 64)
    assert plan.window.vod_end == pytest.approx(10.0 + (9700 + 4 * 64 - 8000) / 64)
    assert plan.clip_start_vod == pytest.approx(16.25)
    kill = plan.events[0]
    assert kill["vod_seconds"] == pytest.approx(20.5)
    assert kill["audio_seconds"] == pytest.approx(20.5 - plan.window.vod_start)
    assert kill["is_pov_kill"] is True


def test_highlights_of_one_round_share_one_alignment(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 1, 8400, 9000), make_sequence(demo, 2, 1, 9400, 9900)])
    first, second = _plan(demo, report, [segment()])
    assert first.status == second.status == "matched"
    key = lambda plan: [(a["demo_tick"], a["vod_seconds"], a["offset_seconds"]) for a in plan.anchors_used]
    assert key(first) == key(second)
    assert (first.stem, second.stem) == ("out-clip01", "out-clip02")


def test_far_from_the_only_anchor_needs_review(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 2, 14800, 15600)])
    (plan,) = _plan(demo, report, [segment()], max_anchor_distance=60.0)
    assert plan.status == "needs_review"
    assert "Add an anchor near round 3" in plan.reasons[0]
    assert plan.window is not None  # candidate times are still reported


def test_a_clip_between_two_anchors_is_matched_even_when_far(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 1, 8400, 9700)])
    two = segment(anchors=[(7000, 100.0), (12000, 100.0 + 5000 / 64)])
    (plan,) = _plan(demo, report, [two], max_anchor_distance=0.0)
    assert plan.status == "matched"
    assert "bracketed_by_consistent_anchors" in plan.basis


def test_round_outside_every_segment_is_unmatched(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 2, 14800, 15600)])
    (plan,) = _plan(demo, report, [segment(rounds=(1, 2))])
    assert plan.status == "unmatched"
    assert plan.window is None
    assert "not covered" in plan.reasons[0]


def test_segment_without_verified_anchors_is_unmatched(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 1, 8400, 9700)])
    (plan,) = _plan(demo, report, [segment(anchors=())])
    assert plan.status == "unmatched"
    assert "no verified anchors" in plan.reasons[0]


def test_padding_is_cut_at_the_segment_edge_and_reported(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 1, 8400, 9700)])
    (plan,) = _plan(demo, report, [segment(anchors=[(8000, 100.0)], rounds=(2, 2))], lead_in=60.0, lead_out=60.0)
    assert plan.window.tick_start == 7000  # round 2 starts here
    assert plan.window.tick_end == 12000 + 320
    assert len(plan.window.clamps) == 2
    assert plan.status == "matched"


def test_vod_bounds_clamp_the_padding_but_reject_a_truncated_clip(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 1, 8400, 9700)])
    (plan,) = _plan(demo, report, [segment(anchors=[(8000, 10.0)])], lead_in=30.0, duration=38.0)
    assert plan.window.vod_start == 0.0
    assert plan.window.vod_end == 38.0
    assert plan.status == "matched"
    (cut_short,) = _plan(demo, report, [segment()], duration=30.0)  # clip itself ends at VOD 36.5
    assert cut_short.status == "unmatched"
    assert "30.0s long" in cut_short.reasons[0]


def test_round_mode_covers_the_whole_round(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 1, 8400, 9700)])
    (plan,) = _plan(demo, report, [segment()], mode="round", lead_in=2.0, lead_out=3.0)
    assert plan.window.tick_start == 7000 - 128
    assert plan.window.tick_end == 12000 + 192
    assert plan.clip_start_vod > plan.window.vod_start  # the highlight sits inside the round audio


def test_clip_without_kill_events_needs_review(tmp_path, demo):
    report = _report(tmp_path, demo, [make_sequence(demo, 1, 1, 9100, 9400)])
    assert report.sequences[0].kill_ticks == []
    (plan,) = _plan(demo, report, [segment()])
    assert plan.status == "needs_review"
    assert "no kill events" in plan.reasons[0]


def test_slow_motion_does_not_change_the_audio_window(tmp_path, demo):
    plain = make_sequence(demo, 1, 1, 8400, 9700)
    slow = make_sequence(demo, 1, 1, 8400, 9700, slowmo={"kill_tick": 9616, "start_seconds": 3, "end_seconds": 5, "speed": 0.4})
    (a,) = _plan(demo, _report(tmp_path, demo, [plain], "a.json"), [segment()])
    (b,) = _plan(demo, _report(tmp_path, demo, [slow], "b.json"), [segment()])
    assert a.window == b.window
    assert [e["audio_seconds"] for e in a.events] == [e["audio_seconds"] for e in b.events]
