import hashlib
import json

import click.testing
import pytest
from rich.console import Console
from broadcast_helpers import make_sequence, make_test_demo, write_anchor_json, write_test_report

import highlights.broadcast.commands as commands
from highlights.cli import main


@pytest.fixture
def env(tmp_path, monkeypatch):
    demo = make_test_demo()
    monkeypatch.setattr(commands, "parse_demo", lambda path: demo)
    monkeypatch.setattr(commands, "_find_ffmpeg", lambda: None)
    monkeypatch.setattr(commands, "console", Console(width=200))  # keep table cells on one line
    dem = tmp_path / "final.dem"
    dem.write_bytes(b"")
    reports = tmp_path / "out"
    reports.mkdir()
    write_test_report(
        reports / "lan-p-1.json", demo, [make_sequence(demo, 1, 1, 8400, 9700)]
    )  # --json-only style: no mp4
    anchors = write_anchor_json(tmp_path / "anchors.json", [("main", None, [(8000, 10.0)])], vod="vod.mp4")
    return click.testing.CliRunner(), tmp_path, dem, reports, anchors, demo


def _run(runner, *args):
    return runner.invoke(main, ["broadcast-sync", *args], catch_exceptions=False)


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_dry_run_predicts_the_window_and_writes_nothing(env):
    runner, tmp_path, dem, reports, anchors, _ = env
    before = sorted(p.name for p in tmp_path.rglob("*"))
    result = _run(runner, "--demo", str(dem), "--reports-dir", str(reports), "--anchors", str(anchors), "--dry-run")
    assert result.exit_code == 0, result.output
    assert "matched" in result.output and "Dry run: 1 matched" in result.output
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_default_leads_are_five_and_eight_seconds(env):
    runner, tmp_path, dem, reports, anchors, _ = env
    result = _run(runner, "--demo", str(dem), "--reports-dir", str(reports), "--anchors", str(anchors), "--dry-run")
    # clip starts at VOD 16.25 (tick 8400), lead-in 5 s -> 11.25; ends tick 9700 -> 36.5625 + 8 s -> 44.5625
    assert "0:00:11.250" in result.output and "0:00:44.562" in result.output


def test_demo_name_must_match_the_anchor_file(env):
    runner, tmp_path, dem, reports, _, _ = env
    other = write_anchor_json(tmp_path / "other.json", [("main", None, [(8000, 10.0)])], demo="semi.dem")
    result = _run(runner, "--demo", str(dem), "--reports-dir", str(reports), "--anchors", str(other), "--dry-run")
    assert result.exit_code != 0
    assert "is for demo 'semi.dem'" in result.output


def test_explicit_report_from_another_demo_fails(env):
    runner, tmp_path, dem, reports, anchors, demo = env
    foreign = make_test_demo(name="semi.dem")
    write_test_report(reports / "foreign.json", foreign, [make_sequence(foreign, 1, 1, 8400, 9700)])
    result = _run(runner, "--demo", str(dem), "--report", str(reports / "foreign.json"), "--anchors", str(anchors), "--dry-run")
    assert result.exit_code != 0
    assert "made from 'semi.dem'" in result.output


def test_reports_dir_skips_other_demos_and_non_reports(env):
    runner, tmp_path, dem, reports, anchors, _ = env
    foreign = make_test_demo(name="semi.dem")
    write_test_report(reports / "foreign.json", foreign, [make_sequence(foreign, 1, 1, 8400, 9700)])
    (reports / "notes.json").write_text("{}")
    (reports / "broll.json").write_text(json.dumps({"kind": "broll", "sequences": [], "demo": "final.dem"}))
    result = _run(runner, "--demo", str(dem), "--reports-dir", str(reports), "--anchors", str(anchors), "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Skipped 1 report(s)" in result.output
    assert "lan-p-1" in result.output and "foreign" not in result.output.split("Skipped")[1]


def test_conflicting_anchors_stop_the_command(env):
    runner, tmp_path, dem, reports, _, _ = env
    bad = write_anchor_json(tmp_path / "bad.json", [("main", None, [(8000, 10.0), (9600, 90.0)])])
    result = _run(runner, "--demo", str(dem), "--reports-dir", str(reports), "--anchors", str(bad), "--dry-run")
    assert result.exit_code != 0
    assert "disagree" in result.output


def test_strict_fails_when_something_is_not_matched(env):
    runner, tmp_path, dem, reports, _, demo = env
    write_test_report(reports / "lan-p-2.json", demo, [make_sequence(demo, 1, 2, 14800, 15600)])
    anchors = write_anchor_json(tmp_path / "round2.json", [("only", (2, 2), [(8000, 10.0)])])
    args = ("--demo", str(dem), "--reports-dir", str(reports), "--anchors", str(anchors), "--dry-run")
    assert _run(runner, *args).exit_code == 0
    strict = _run(runner, *args, "--strict")
    assert strict.exit_code != 0 and "not matched" in strict.output


def test_needs_either_a_report_or_a_folder(env):
    runner, tmp_path, dem, _, anchors, _ = env
    result = _run(runner, "--demo", str(dem), "--anchors", str(anchors))
    assert result.exit_code != 0 and "--report" in result.output


def test_missing_vod_is_an_error_outside_dry_run(env):
    runner, tmp_path, dem, reports, anchors, _ = env
    result = _run(runner, "--demo", str(dem), "--reports-dir", str(reports), "--anchors", str(anchors))
    assert result.exit_code != 0
    assert "ffmpeg not found" in result.output


def test_originals_are_never_modified(env):
    runner, tmp_path, dem, reports, anchors, _ = env
    watched = [dem, reports / "lan-p-1.json", anchors]
    before = [_digest(p) for p in watched]
    _run(runner, "--demo", str(dem), "--reports-dir", str(reports), "--anchors", str(anchors), "--dry-run")
    assert [_digest(p) for p in watched] == before


# --- broadcast-anchor ---------------------------------------------------------------


def _anchor(runner, dem, anchors, *args):
    return runner.invoke(
        main, ["broadcast-anchor", "--demo", str(dem), "--anchors", str(anchors), *args], catch_exceptions=False
    )


def test_anchor_add_creates_and_extends_the_file(env, tmp_path):
    runner, _, dem, _, _, _ = env
    path = tmp_path / "new.json"
    first = _anchor(runner, dem, path, "--round", "2", "--event", "freeze_end", "--vod-time", "0:10.0", "--vod", "vod.mp4")
    assert first.exit_code == 0, first.output
    second = _anchor(runner, dem, path, "--round", "2", "--event", "kill", "--kill", "2", "--vod-time", "0:35.25")
    assert second.exit_code == 0, second.output
    data = json.loads(path.read_text())
    assert data["demo"] == "final.dem" and data["vod"] == "vod.mp4"
    assert [(a["demo_tick"], a["vod_seconds"], a["event"]) for a in data["segments"][0]["anchors"]] == [
        (8000, 10.0, "freeze_end"),
        (9616, 35.25, "kill"),
    ]


def test_conflicting_anchor_is_refused_and_the_file_is_unchanged(env, tmp_path):
    runner, _, dem, _, anchors, _ = env
    before = anchors.read_text()
    result = _anchor(runner, dem, anchors, "--round", "2", "--event", "kill", "--kill", "2", "--vod-time", "5:00")
    assert result.exit_code != 0 and "disagree" in result.output
    assert anchors.read_text() == before


def test_anchor_add_rejects_bad_input(env, tmp_path):
    runner, _, dem, _, _, _ = env
    path = tmp_path / "x.json"
    assert "does not exist" in _anchor(runner, dem, path, "--round", "9", "--event", "round_start", "--vod-time", "1:00").output
    assert "Cannot read VOD time" in _anchor(runner, dem, path, "--round", "2", "--event", "round_start", "--vod-time", "soon").output
    assert "needs --kill" in _anchor(runner, dem, path, "--round", "2", "--event", "kill", "--vod-time", "1:00").output
    assert not path.exists()


def test_anchor_add_second_segment_after_a_break(env, tmp_path):
    runner, _, dem, _, _, _ = env
    path = tmp_path / "split.json"
    one = _anchor(runner, dem, path, "--round", "2", "--event", "freeze_end", "--vod-time", "10", "--segment", "h1", "--rounds", "1-2")
    two = _anchor(runner, dem, path, "--round", "4", "--event", "freeze_end", "--vod-time", "900", "--segment", "h2", "--rounds", "3-4")
    assert one.exit_code == two.exit_code == 0, two.output
    assert [s["id"] for s in json.loads(path.read_text())["segments"]] == ["h1", "h2"]
