"""Cut a cinematic take into the POV clip of the same play."""

from __future__ import annotations

from pathlib import Path

from highlights.video.ffmpeg import run_ffmpeg


def splice_filter(segments: list[tuple[float, float]]) -> str:
    """Overlay the cinematic video on the POV video while inside any segment.

    Both clips cover the same ticks and share resolution and fps, so the overlay
    is a hard cut; the output keeps the POV timeline (and length)."""
    enable = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in segments)
    return f"[0:v][1:v]overlay=enable='{enable}':eof_action=pass,format=yuv420p[v]"


def splice_clip(
    ffmpeg_exe: Path,
    pov_clip: Path,
    cinematic_clip: Path,
    segments: list[tuple[float, float]],
    output_path: Path,
    crf: int = 18,
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
            splice_filter(segments),
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
