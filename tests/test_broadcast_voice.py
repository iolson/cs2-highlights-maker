"""--isolate-voice: demucs is faked (it writes a tone where the real one writes vocals.wav)."""

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
import highlights.broadcast.voice as voice
from highlights.broadcast.models import BroadcastSyncError
from highlights.cli import main

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe not installed")

STEM_RATE = 44100  # demucs writes 44.1 kHz


def _write_stem(path: Path, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = [int(8000 * math.sin(2 * math.pi * 440 * i / STEM_RATE)) for i in range(round(seconds * STEM_RATE))]
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(STEM_RATE)
        handle.writeframes(b"".join(struct.pack("<hh", f, f) for f in frames))


@pytest.fixture
def fake_demucs(monkeypatch):
    calls = []
    real_run = subprocess.run

    def run(command, **kwargs):
        if "demucs" not in command:  # ffmpeg/ffprobe share the patched module
            return real_run(command, **kwargs)
        calls.append(command)
        wav = Path(command[-1])
        work = Path(command[command.index("-o") + 1])
        with wave.open(str(wav), "rb") as handle:
            seconds = handle.getnframes() / handle.getframerate()
        _write_stem(work / voice.MODEL / wav.stem / "vocals.wav", seconds)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(voice, "available", lambda: True)
    monkeypatch.setattr(voice.subprocess, "run", run)
    return calls


def _cut(path: Path, seconds: float = 3.0) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * 2 * round(seconds * 48000))


def test_voice_stem_is_48k_stereo_and_the_same_length(fake_demucs, tmp_path):
    cut, out = tmp_path / "a-broadcast.wav", tmp_path / "a-broadcast-voice.wav"
    _cut(cut)
    voice.isolate_voice(Path(FFMPEG), cut, out, "cpu")
    with wave.open(str(out), "rb") as handle:
        assert (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) == (48000, 2, 2)
        assert handle.getnframes() / 48000 == pytest.approx(3.0, abs=0.02)
    assert fake_demucs[0][fake_demucs[0].index("-d") + 1] == "cpu"
    assert "--two-stems=vocals" in fake_demucs[0]
    assert not list(tmp_path.glob("*.part"))


def test_voice_path_sits_beside_the_cut():
    assert voice.voice_path(Path("x/clip-broadcast.wav")) == Path("x/clip-broadcast-voice.wav")


def test_demucs_failure_is_a_readable_error(monkeypatch, tmp_path):
    real_run = subprocess.run
    monkeypatch.setattr(voice, "available", lambda: True)
    monkeypatch.setattr(
        voice.subprocess,
        "run",
        lambda command, **kw: (
            subprocess.CompletedProcess(command, 1, "", "boom\nOut of memory") if "demucs" in command else real_run(command, **kw)
        ),
    )
    cut = tmp_path / "a-broadcast.wav"
    _cut(cut)
    with pytest.raises(BroadcastSyncError, match="Out of memory"):
        voice.isolate_voice(Path(FFMPEG), cut, tmp_path / "v.wav", "cpu")


def test_missing_demucs_names_the_install_extra(monkeypatch):
    monkeypatch.setattr(voice, "available", lambda: False)
    with pytest.raises(BroadcastSyncError, match=r"cs2-highlights\[voice\]"):
        voice.require()


# --- the command --------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    demo = make_test_demo()
    monkeypatch.setattr(commands, "parse_demo", lambda path: demo)
    monkeypatch.setattr(commands, "_find_ffmpeg", lambda: Path(FFMPEG))
    vod = tmp_path / "vod.mp4"
    subprocess.run(
        [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x90:r=10:d=60",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=60", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(vod)],
        check=True,
    )  # fmt: skip
    dem = tmp_path / "final.dem"
    dem.write_bytes(b"demo")
    out = tmp_path / "out"
    out.mkdir()
    write_test_report(out / "lan-p-1.json", demo, [make_sequence(demo, 1, 1, 8400, 9700)])
    anchors = write_anchor_json(tmp_path / "anchors.json", [("main", None, [(8000, 10.0)])], vod=str(vod))
    return dem, out, anchors


def _sync(dem, out, anchors, *extra):
    args = ["broadcast-sync", "--demo", str(dem), "--reports-dir", str(out), "--anchors", str(anchors), *extra]
    return click.testing.CliRunner().invoke(main, args, catch_exceptions=False)


def test_flag_writes_a_voice_wav_and_records_it_in_the_sidecar(project, fake_demucs):
    dem, out, anchors = project
    result = _sync(dem, out, anchors, "--isolate-voice", "--voice-device", "cpu")
    assert result.exit_code == 0, result.output
    data = json.loads((out / "lan-p-1-broadcast-sync.json").read_text())
    block = data["audio"]["voice"]
    assert block["filename"] == "lan-p-1-broadcast-voice.wav"
    assert (block["sample_rate"], block["channels"], block["model"]) == (48000, 2, voice.MODEL)
    assert block["measured_duration_seconds"] == pytest.approx(data["audio"]["measured_duration_seconds"], abs=0.02)
    assert (out / "lan-p-1-broadcast-voice.wav").exists() and (out / "lan-p-1-broadcast.wav").exists()
    assert data["diagnostics"] == []


def test_without_the_flag_demucs_is_never_run(project, fake_demucs):
    dem, out, anchors = project
    assert _sync(dem, out, anchors).exit_code == 0
    assert fake_demucs == []
    assert "voice" not in json.loads((out / "lan-p-1-broadcast-sync.json").read_text())["audio"]
    assert not (out / "lan-p-1-broadcast-voice.wav").exists()


def test_missing_demucs_stops_before_any_cutting(project, monkeypatch):
    dem, out, anchors = project
    monkeypatch.setattr(voice, "available", lambda: False)
    result = click.testing.CliRunner().invoke(
        main, ["broadcast-sync", "--demo", str(dem), "--reports-dir", str(out), "--anchors", str(anchors), "--isolate-voice"]
    )
    assert result.exit_code != 0 and "cs2-highlights[voice]" in result.output
    assert not (out / "lan-p-1-broadcast.wav").exists()


def test_dry_run_does_not_need_demucs(project, monkeypatch):
    dem, out, anchors = project
    monkeypatch.setattr(voice, "available", lambda: False)
    assert _sync(dem, out, anchors, "--isolate-voice", "--dry-run").exit_code == 0
