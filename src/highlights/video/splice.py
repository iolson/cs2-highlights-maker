"""Cut a cinematic take into the POV clip of the same play."""

from __future__ import annotations

from pathlib import Path

from highlights.video.ffmpeg import run_ffmpeg
from highlights.video.tiltshift import tilt_filter

AUDIO_MODES = ("pov", "camera", "mix")
# (POV level, camera level) inside the cinematic segments; outside them the POV plays alone at 1.0.
# mix sums to 1.0 so it never gets louder than either take.
AUDIO_LEVELS = {"camera": (0.0, 1.0), "mix": (0.3, 0.7)}
# Gain ramp at each cut, seconds: short enough to follow the hard picture cut, long enough not to click.
AUDIO_FADE = 0.05
# Audio frames are split this small so the per-frame volume expression ramps smoothly (~1.3 ms at 48 kHz).
AUDIO_FRAME_SAMPLES = 64


def splice_filter(segments: list[tuple[float, float]], tilt: tuple[int, int] | None = None) -> str:
    """Overlay the cinematic video on the POV video while inside any segment.

    Both clips cover the same ticks and share resolution and fps, so the overlay
    is a hard cut; the output keeps the POV timeline (and length). `tilt` (footage width, height)
    gives the cinematic footage a tilt-shift look first."""
    enable = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in segments)
    cinematic = f"[1:v]{tilt_filter(*tilt)}[cin];[0:v][cin]" if tilt else "[0:v][1:v]"
    return f"{cinematic}overlay=enable='{enable}':eof_action=pass,format=yuv420p[v]"


def segment_envelope(segments: list[tuple[float, float]], fade: float = AUDIO_FADE) -> str:
    """FFmpeg expression of t: 1 inside the segments, 0 outside, linear ramps of `fade` just inside each edge."""
    parts = [
        f"min(1,max(0,(t-{a:.3f})/{fade}))*min(1,max(0,({b:.3f}-t)/{fade}))"
        for a, b in segments
    ]
    return f"min(1,{'+'.join(parts)})"


def audio_filter(segments: list[tuple[float, float]], mode: str) -> str:
    """POV audio outside the segments, the cinematic take's audio (camera) or both (mix) inside them.

    Each input's gain follows the segment envelope; the take stays aligned with the POV
    because both recordings cover the same ticks. Output length = the POV's."""
    pov_level, cam_level = AUDIO_LEVELS[mode]
    g = segment_envelope(segments)
    split = f"asetnsamples=n={AUDIO_FRAME_SAMPLES}:p=0"
    return (
        f"[0:a]{split},volume='1-({g})*{1 - pov_level}':eval=frame[pa];"
        f"[1:a]{split},volume='({g})*{cam_level}':eval=frame[ca];"
        "[pa][ca]amix=inputs=2:duration=first:normalize=0[a]"
    )


def splice_clip(
    ffmpeg_exe: Path,
    pov_clip: Path,
    cinematic_clip: Path,
    segments: list[tuple[float, float]],
    output_path: Path,
    crf: int = 18,
    tilt: tuple[int, int] | None = None,
    audio: str = "pov",
) -> Path:
    """Write output_path: cinematic picture inside the segments; POV audio throughout (`audio` "pov"),
    or the take's audio inside the segments ("camera") or both ("mix")."""
    if audio not in AUDIO_MODES:
        raise ValueError(f"audio must be one of {AUDIO_MODES}, not {audio!r}")
    graph = splice_filter(segments, tilt)
    if audio == "pov":
        audio_args = ["-map", "0:a:0", "-c:a", "copy"]
    else:
        graph += ";" + audio_filter(segments, audio)
        audio_args = ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    run_ffmpeg(
        ffmpeg_exe,
        [
            "-y",
            "-i",
            str(pov_clip),
            "-i",
            str(cinematic_clip),
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            *audio_args,
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            str(output_path),
        ],
    )
    return output_path
