import json

import pytest
from broadcast_helpers import ROUND2_FREEZE_END, TR, anchor_file, make_test_demo, segment, write_anchor_json

from highlights.broadcast.anchors import (
    add_anchor,
    event_tick,
    format_timestamp,
    load_anchor_file,
    parse_timestamp,
)
from highlights.broadcast.models import Anchor, BroadcastSyncError
from highlights.broadcast.timeline import Timeline


def _timeline(*segments, tolerance=0.5):
    return Timeline(make_test_demo(), anchor_file("a.json", list(segments)), tolerance)


def test_single_anchor_maps_ticks_at_demo_tickrate():
    timeline = _timeline(segment(anchors=[(ROUND2_FREEZE_END, 100.0)]))
    seg = timeline.segments[0]
    assert timeline.to_vod(seg, ROUND2_FREEZE_END) == 100.0
    assert timeline.to_vod(seg, ROUND2_FREEZE_END + 640) == pytest.approx(110.0)
    assert timeline.to_vod(seg, ROUND2_FREEZE_END - 64) == pytest.approx(99.0)


def test_tickrate_comes_from_the_demo_not_64():
    demo = make_test_demo()
    demo.tickrate = 128.0
    timeline = Timeline(demo, anchor_file("a.json", [segment(anchors=[(ROUND2_FREEZE_END, 100.0)])]))
    assert timeline.to_vod(timeline.segments[0], ROUND2_FREEZE_END + 640) == pytest.approx(105.0)


def test_two_consistent_anchors_validate_and_nearest_one_maps():
    # 9600 - 8000 = 1600 ticks = 25 s; the second anchor is 0.2 s late, inside the tolerance.
    timeline = _timeline(segment(anchors=[(8000, 100.0), (9600, 125.2)]))
    seg = timeline.segments[0]
    assert timeline.to_vod(seg, 8100) == pytest.approx(100.0 + 100 / TR)
    assert timeline.to_vod(seg, 9500) == pytest.approx(125.2 - 100 / TR)


def test_conflicting_anchors_are_rejected_not_averaged():
    with pytest.raises(BroadcastSyncError, match="disagree.*split the segment"):
        _timeline(segment(anchors=[(8000, 100.0), (9600, 140.0)]))


def test_vod_going_backwards_is_rejected():
    with pytest.raises(BroadcastSyncError, match="backwards"):
        _timeline(segment(anchors=[(8000, 100.0), (9600, 90.0)]), tolerance=1000)


def test_segments_do_not_bridge_a_break():
    # Halftime-style: round 2 and round 3 sit in different segments with different offsets
    # (a 400 s desk segment between them). Each maps through its own anchor only.
    first = segment("first", anchors=[(8000, 100.0)], rounds=(1, 2))
    second = segment("second", anchors=[(14000, 600.0)], rounds=(3, 4))
    timeline = _timeline(first, second)
    assert timeline.segment_for_round(2).id == "first"
    assert timeline.segment_for_round(3).id == "second"
    assert timeline.to_vod(first, 12000) == pytest.approx(100.0 + 4000 / TR)
    assert timeline.to_vod(second, 15000) == pytest.approx(600.0 + 1000 / TR)


def test_round_without_a_segment_is_uncovered():
    timeline = _timeline(segment("only", anchors=[(8000, 100.0)], rounds=(2, 2)))
    assert timeline.segment_for_round(1) is None
    assert timeline.segment_for_round(3) is None


def test_overlapping_segments_are_rejected():
    with pytest.raises(BroadcastSyncError, match="overlap"):
        _timeline(segment("a", rounds=(1, 3)), segment("b", anchors=[(14000, 500.0)], rounds=(3, 4)))


def test_round_range_outside_the_demo_is_rejected():
    with pytest.raises(BroadcastSyncError, match="not valid for a demo with 4 rounds"):
        _timeline(segment(rounds=(2, 9)))


def test_anchor_outside_its_segments_rounds_is_rejected():
    with pytest.raises(BroadcastSyncError, match="outside the segment"):
        _timeline(segment(anchors=[(14000, 500.0)], rounds=(1, 2)))


def test_unverified_anchors_are_ignored():
    seg = segment(anchors=[(8000, 100.0)])
    seg = seg.__class__(seg.id, seg.kind, None, None, (*seg.anchors, Anchor(9600, 999.0, verified=False)))
    timeline = _timeline(seg)
    assert [a.demo_tick for a in timeline.usable_anchors(timeline.segments[0])] == [8000]
    assert timeline.ignored[0][1].demo_tick == 9600


def test_segment_tick_range_ends_shortly_after_the_last_round():
    timeline = _timeline(segment(rounds=(2, 2)))
    low, high = timeline.tick_range(timeline.segments[0])
    assert low == 7000
    assert high == 12000 + 320  # 5 s after round_end, still before round 3 starts


def test_timestamps_parse_and_format():
    assert parse_timestamp("1:02:03.5") == 3723.5
    assert parse_timestamp("12:03") == 723.0
    assert parse_timestamp("723.4") == 723.4
    assert format_timestamp(3723.5) == "1:02:03.500"
    for bad in ("abc", "-5", "1:75", "1:02:75"):
        with pytest.raises(BroadcastSyncError):
            parse_timestamp(bad)


def test_event_ticks_come_from_the_demo():
    demo = make_test_demo()
    assert event_tick(demo, 2, "round_start") == 7000
    assert event_tick(demo, 2, "freeze_end") == 8000
    assert event_tick(demo, 2, "round_end") == 12000
    assert event_tick(demo, 2, "kill", kill=2) == 9616
    with pytest.raises(BroadcastSyncError, match="needs --kill"):
        event_tick(demo, 2, "kill")
    with pytest.raises(BroadcastSyncError, match="has 2 kills"):
        event_tick(demo, 2, "kill", kill=3)
    with pytest.raises(BroadcastSyncError, match="does not exist"):
        event_tick(demo, 9, "round_start")
    with pytest.raises(BroadcastSyncError, match="only goes with"):
        event_tick(demo, 2, "round_start", kill=1)


def test_add_anchor_validates_before_returning():
    demo = make_test_demo()
    base = anchor_file("a.json", [segment(anchors=[(8000, 100.0)])])
    updated = add_anchor(base, demo, "main", Anchor(9600, 125.0))
    assert [a.demo_tick for a in updated.segments[0].anchors] == [8000, 9600]
    assert len(base.segments[0].anchors) == 1  # input untouched
    with pytest.raises(BroadcastSyncError, match="disagree"):
        add_anchor(base, demo, "main", Anchor(9600, 160.0))
    with pytest.raises(BroadcastSyncError, match="already has an anchor"):
        add_anchor(base, demo, "main", Anchor(8000, 100.0))


# --- anchor file loading -----------------------------------------------------------


def test_load_anchor_file_round_trip(tmp_path):
    path = write_anchor_json(tmp_path / "a.json", [("main", None, [(9600, 125.0), (8000, 100.0)])], vod="v.mp4")
    loaded = load_anchor_file(path)
    assert loaded.vod == "v.mp4"
    assert [a.demo_tick for a in loaded.segments[0].anchors] == [8000, 9600]  # sorted


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(schema_version=2), "schema_version"),
        (lambda d: d.pop("demo"), "'demo'"),
        (lambda d: d.update(segments=[]), "non-empty"),
        (lambda d: d["segments"][0].update(kind="replay"), "not supported"),
        (lambda d: d["segments"][0].pop("id"), "'id'"),
        (lambda d: d["segments"][0]["anchors"][0].update(demo_tick="x"), "demo_tick"),
        (lambda d: d["segments"][0]["anchors"][0].update(vod_seconds=-1), "vod_seconds"),
        (lambda d: d["segments"][0].update(round_start=0), "round_start"),
    ],
)
def test_malformed_anchor_files_fail_clearly(tmp_path, mutate, message):
    path = write_anchor_json(tmp_path / "a.json", [("main", None, [(8000, 100.0)])])
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))
    with pytest.raises(BroadcastSyncError, match=message):
        load_anchor_file(path)


def test_missing_and_unparseable_anchor_files(tmp_path):
    with pytest.raises(BroadcastSyncError, match="not found"):
        load_anchor_file(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(BroadcastSyncError, match="Cannot read"):
        load_anchor_file(bad)
