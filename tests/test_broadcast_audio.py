"""FFmpeg smoke tests on a synthetic VOD: silence with 1 kHz beeps at known times.

Round 2 freezes end at demo tick 8000 = the beep at VOD 10.0 s (the anchor); the kills at
ticks 8672 and 9616 sit on the beeps at 20.5 s and 35.25 s. A beep is a transient we can
find in the exported WAV to check the cut against the plan.
"""

import hashlib
import json
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import click.testing
import pytest
from broadcast_helpers import make_sequence, make_test_demo, write_anchor_json, write_test_report

import highlights.broadcast.commands as commands
from highlights.broadcast.audio import extract_audio, probe_media
from highlights.broadcast.models import BroadcastSyncError
from highlights.cli import main

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe not installed")

RATE = 48000
VOD_SECONDS = 90
BEEPS = [10.0, 20.5, 35.25, 60.0]
TOLERANCE = 0.05  # acceptance: transient placement within 50 ms


def _write_beeps(path: Path) -> None:
    samples = [0] * (RATE * VOD_SECONDS)
    for at in BEEPS:
        start = round(at * RATE)
        for i in range(RATE // 10):  # 100 ms beep
            samples[start + i] = int(20000 * math.sin(2 * math.pi * 1000 * i / RATE))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))


@pytest.fixture(scope="module")
def vod(tmp_path_factory):
    folder = tmp_path_factory.mktemp("vod")
    beeps = folder / "beeps.wav"
    _write_beeps(beeps)
    out = folder / "broadcast.mp4"
    subprocess.run(
        [
            FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s=160x90:r=10:d={VOD_SECONDS}",
            "-i", str(beeps), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "128k",
            "-shortest", str(out),
        ],
        check=True,
    )  # fmt: skip
    silent = folder / "silent.mp4"
    subprocess.run(
        [FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s=160x90:r=10:d=5", "-c:v", "libx264", str(silent)],
        check=True,
    )
    return out, silent


def _onsets(path: Path) -> list[float]:
    """Times (s) where a beep starts in the WAV (first sample above half of the peak after silence)."""
    with wave.open(str(path), "rb") as handle:
        channels, rate = handle.getnchannels(), handle.getframerate()
        data = struct.unpack(f"<{handle.getnframes() * channels}h", handle.readframes(handle.getnframes()))
    mono = data[::channels]
    threshold = max(abs(s) for s in mono) * 0.5
    onsets, quiet_since = [], 0
    for i, sample in enumerate(mono):
        if abs(sample) > threshold:
            if i - quiet_since > rate * 0.2 or not onsets:
                onsets.append(i / rate)
            quiet_since = i
    return onsets


def test_probe_reads_duration_and_audio(vod):
    media = probe_media(Path(FFMPEG), vod[0])
    assert media.duration == pytest.approx(VOD_SECONDS, abs=0.2)
    assert media.audio_streams[0]["sample_rate"] == "48000"


def test_video_without_audio_is_refused(vod):
    with pytest.raises(BroadcastSyncError, match="no audio stream"):
        probe_media(Path(FFMPEG), vod[1])


def test_missing_vod_is_refused(tmp_path):
    with pytest.raises(BroadcastSyncError, match="VOD not found"):
        probe_media(Path(FFMPEG), tmp_path / "nope.mp4")


def test_extract_is_sample_accurate_pcm48k_stereo(vod, tmp_path):
    out = tmp_path / "cut.wav"
    extract_audio(Path(FFMPEG), vod[0], 13.25, 27.25, out)
    with wave.open(str(out), "rb") as handle:
        assert (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) == (48000, 2, 2)
        assert handle.getnframes() / 48000 == pytest.approx(27.25, abs=0.02)
    onsets = _onsets(out)
    assert len(onsets) == 2  # the beeps at 10 s and 60 s are outside the cut
    assert onsets[0] == pytest.approx(20.5 - 13.25, abs=TOLERANCE)
    assert onsets[1] == pytest.approx(35.25 - 13.25, abs=TOLERANCE)
    assert not list(tmp_path.glob("*.part"))


# --- the whole command --------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch, vod):
    demo = make_test_demo()
    monkeypatch.setattr(commands, "parse_demo", lambda path: demo)
    monkeypatch.setattr(commands, "_find_ffmpeg", lambda: Path(FFMPEG))
    dem = tmp_path / "final.dem"
    dem.write_bytes(b"demo")
    out = tmp_path / "out"
    out.mkdir()
    # Two highlights in round 2, one kill each is on a beep: kills at 8672 and 9616.
    write_test_report(out / "lan-p-1.json", demo, [make_sequence(demo, 1, 1, 8400, 9700)])
    anchors = write_anchor_json(tmp_path / "anchors.json", [("main", None, [(8000, 10.0)])], vod=str(vod[0]))
    return tmp_path, dem, out, anchors, vod[0]


def _sync(*args):
    return click.testing.CliRunner().invoke(main, ["broadcast-sync", *args], catch_exceptions=False)


def test_export_places_the_commentary_on_the_kills(project):
    tmp_path, dem, out, anchors, vod = project
    result = _sync("--demo", str(dem), "--reports-dir", str(out), "--anchors", str(anchors), "--lead-in", "3", "--lead-out", "4")
    assert result.exit_code == 0, result.output

    wav, sidecar = out / "lan-p-1-broadcast.wav", out / "lan-p-1-broadcast-sync.json"
    data = json.loads(sidecar.read_text())
    assert data["status"] == "matched"
    assert data["audio"]["filename"] == wav.name
    assert (data["audio"]["sample_rate"], data["audio"]["channels"], data["audio"]["format"]) == (48000, 2, "pcm_s16le")
    assert data["audio"]["vod_start_seconds"] == pytest.approx(13.25)
    assert data["audio"]["measured_duration_seconds"] == pytest.approx(data["audio"]["duration_seconds"], abs=0.02)
    # Highlight starts 3 s into the audio (the lead-in); the kills are where the beeps are.
    assert data["alignment"]["audio_offset_at_highlight_start_seconds"] == pytest.approx(3.0)
    kills = [e["audio_seconds"] for e in data["events"]]
    onsets = _onsets(wav)
    assert len(onsets) == len(kills) == 2
    for predicted, heard in zip(kills, onsets):
        assert heard == pytest.approx(predicted, abs=TOLERANCE)
    assert data["timing"]["nominal_clip_seconds"] == pytest.approx(1300 / 64, abs=0.001)


def test_dry_run_with_a_real_vod_writes_no_audio(project):
    tmp_path, dem, out, anchors, vod = project
    before = sorted(p.name for p in tmp_path.rglob("*"))
    result = _sync("--demo", str(dem), "--reports-dir", str(out), "--anchors", str(anchors), "--dry-run")
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_padding_is_clamped_to_the_vod(project):
    tmp_path, dem, out, anchors, vod = project
    result = _sync("--demo", str(dem), "--reports-dir", str(out), "--anchors", str(anchors), "--lead-in", "100", "--lead-out", "100")
    assert result.exit_code == 0, result.output
    data = json.loads((out / "lan-p-1-broadcast-sync.json").read_text())
    assert data["audio"]["vod_start_seconds"] == 0.0
    assert data["audio"]["vod_end_seconds"] == pytest.approx(VOD_SECONDS, abs=0.2)
    assert any("VOD" in note for note in data["clamps"])
    # The cut starts at VOD 0, so the highlight start sits at its VOD time within the audio.
    assert data["alignment"]["audio_offset_at_highlight_start_seconds"] == pytest.approx(16.25)
    assert _onsets(out / "lan-p-1-broadcast.wav")[1] == pytest.approx(20.5, abs=TOLERANCE)


def test_output_is_deterministic_and_inputs_untouched(project):
    tmp_path, dem, out, anchors, vod = project
    watched = [dem, out / "lan-p-1.json", anchors, vod]
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    before = [digest(p) for p in watched]
    args = ("--demo", str(dem), "--reports-dir", str(out), "--anchors", str(anchors))
    _sync(*args)
    first = [digest(out / "lan-p-1-broadcast.wav"), digest(out / "lan-p-1-broadcast-sync.json")]
    _sync(*args)
    assert [digest(out / "lan-p-1-broadcast.wav"), digest(out / "lan-p-1-broadcast-sync.json")] == first
    assert [digest(p) for p in watched] == before


def test_needs_review_is_not_exported_unless_allowed(project):
    tmp_path, dem, out, _, vod = project
    # Anchor at the end of round 1 (VOD 10 s); the round-2 clip is 37 s of game time away.
    far = write_anchor_json(tmp_path / "far.json", [("main", None, [(6000, 10.0)])], vod=str(vod))
    args = ("--demo", str(dem), "--reports-dir", str(out), "--anchors", str(far), "--max-anchor-distance", "10")
    assert _sync(*args).exit_code == 0
    review = json.loads((out / "lan-p-1-broadcast-sync.json").read_text())
    assert review["status"] == "needs_review" and review["review"]["required"] is True
    assert review["audio"]["filename"] is None and not (out / "lan-p-1-broadcast.wav").exists()

    assert _sync(*args, "--allow-review").exit_code == 0
    assert (out / "lan-p-1-broadcast.wav").exists()
    assert json.loads((out / "lan-p-1-broadcast-sync.json").read_text())["status"] == "needs_review"


def test_clip_beyond_a_truncated_vod_is_unmatched_without_audio(project, tmp_path):
    tmp_path, dem, out, anchors, vod = project
    short = tmp_path / "short.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-i", str(vod), "-t", "30", "-c", "copy", str(short)], check=True)
    result = _sync("--demo", str(dem), "--reports-dir", str(out), "--anchors", str(anchors), "--vod", str(short))
    assert result.exit_code == 0, result.output
    data = json.loads((out / "lan-p-1-broadcast-sync.json").read_text())
    assert data["status"] == "unmatched" and data["audio"] is None
    assert not (out / "lan-p-1-broadcast.wav").exists()


def test_vod_without_audio_stops_the_command(project, vod):
    tmp_path, dem, out, anchors, _ = project
    result = _sync("--demo", str(dem), "--reports-dir", str(out), "--anchors", str(anchors), "--vod", str(vod[1]))
    assert result.exit_code != 0 and "no audio stream" in result.output


def test_anchor_file_vod_resolves_beside_the_anchor_file(project, tmp_path):
    tmp_path, dem, out, _, vod = project
    folder = tmp_path / "anchors-here"
    folder.mkdir()
    shutil.copy(vod, folder / "vod.mp4")
    anchors = write_anchor_json(folder / "a.json", [("main", None, [(8000, 10.0)])], vod="vod.mp4")
    result = _sync("--demo", str(dem), "--reports-dir", str(out), "--anchors", str(anchors))
    assert result.exit_code == 0, result.output
    assert (out / "lan-p-1-broadcast.wav").exists()
