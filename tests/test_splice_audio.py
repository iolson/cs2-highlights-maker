"""--cinematic-audio: which take's sound plays inside the spliced segments."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from highlights.cli import _splice_cinematics, main
from highlights.video.splice import audio_filter, segment_envelope, splice_clip


def envelope_at(expr: str, t: float) -> float:
    return eval(expr, {"min": min, "max": max, "t": t})


def test_envelope_is_one_inside_zero_outside_and_ramps_at_the_cuts():
    expr = segment_envelope([(1.0, 2.0), (3.0, 4.0)])
    assert envelope_at(expr, 0.5) == 0 and envelope_at(expr, 2.5) == 0 and envelope_at(expr, 5) == 0
    assert envelope_at(expr, 1.5) == 1 and envelope_at(expr, 3.5) == 1
    assert envelope_at(expr, 1.025) == pytest.approx(0.5)
    assert envelope_at(expr, 3.975) == pytest.approx(0.5)


def test_audio_filter_levels():
    camera = audio_filter([(1.0, 2.0)], "camera")
    assert "*1.0':eval=frame[pa]" in camera and "*1.0':eval=frame[ca]" in camera
    mix = audio_filter([(1.0, 2.0)], "mix")
    assert "*0.7':eval=frame[pa]" in mix and "*0.7':eval=frame[ca]" in mix  # POV drops to 0.3, camera at 0.7
    assert "duration=first" in mix and "normalize=0" in mix


def test_splice_chain_passes_audio_mode(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        "highlights.video.splice.splice_clip",
        lambda ffmpeg, clip, take, segments, out, crf, tilt=None, audio="pov": calls.append(audio) or out,
    )
    pov = SimpleNamespace(number=1, cinematic_of=None, splice_segments=None, dynamic_cam=None, static_downtime=None, tilt=False)
    take = SimpleNamespace(number=2, cinematic_of=1, splice_segments=[(1.0, 2.0)], dynamic_cam=None, static_downtime=None, tilt=False)
    recorded = [(SimpleNamespace(sequence=pov), Path("pov.mp4")), (SimpleNamespace(sequence=take), Path("cin.mp4"))]
    _splice_cinematics(Path("ffmpeg"), recorded, tmp_path, 18, audio="mix")
    assert calls == ["mix"]


def test_cinematic_audio_needs_splice_cinematic(tmp_path):
    demo = tmp_path / "x.dem"
    demo.write_bytes(b"")
    result = CliRunner().invoke(main, ["run", str(demo), "--cinematic-audio", "camera"])
    assert result.exit_code != 0 and "--cinematic-audio needs" in result.output


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.parametrize("mode", ["camera", "mix"])
def test_camera_audio_splice_runs_end_to_end(tmp_path, mode):
    ffmpeg = Path(shutil.which("ffmpeg"))
    clips = []
    for name, source, freq in (("pov.mp4", "testsrc", 440), ("cin.mp4", "smptebars", 880)):
        clip = tmp_path / name
        subprocess.run(
            [str(ffmpeg), "-loglevel", "error", "-f", "lavfi", "-i", f"{source}=size=320x180:rate=30:duration=3",
             "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=3:sample_rate=48000", "-shortest",
             "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)],
            check=True,
        )
        clips.append(clip)
    out = splice_clip(ffmpeg, clips[0], clips[1], [(1.0, 2.0)], tmp_path / "out.mp4", audio=mode)
    probe = subprocess.run(
        [str(ffmpeg), "-i", str(out), "-map", "0:a", "-f", "null", "-"], capture_output=True, text=True
    )
    assert probe.returncode == 0 and "Audio:" in probe.stderr
