"""Cut a cinematic take into the POV clip of the same play."""

from __future__ import annotations

from pathlib import Path

from highlights.video.ffmpeg import run_ffmpeg
from highlights.video.tiltshift import tilt_filter


def splice_filter(segments: list[tuple[float, float]], tilt: bool = False) -> str:
    """Overlay the cinematic video on the POV video while inside any segment.

    Both clips cover the same ticks and share resolution and fps, so the overlay
    is a hard cut; the output keeps the POV timeline (and length). `tilt` gives the cinematic
    footage a tilt-shift look first."""
    enable = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in segments)
    cinematic = f"[1:v]{tilt_filter()}[cin];[0:v][cin]" if tilt else "[0:v][1:v]"
    return f"{cinematic}overlay=enable='{enable}':eof_action=pass,format=yuv420p[v]"


def splice_clip(
    ffmpeg_exe: Path,
    pov_clip: Path,
    cinematic_clip: Path,
    segments: list[tuple[float, float]],
    output_path: Path,
    crf: int = 18,
    tilt: bool = False,
) -> Path:
    """Write output_path: POV audio throughout, cinematic picture inside the segments."""
    run_ffmpeg(
        ffmpeg_exe,
        [
            "-y",
            "-i",
            str(pov_clip),
            "-i",
            str(cinematic_clip),
            "-filter_complex",
            splice_filter(segments, tilt),
            "-map",
            "[v]",
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            "-c:a",
            "copy",
            str(output_path),
        ],
    )
    return output_path
