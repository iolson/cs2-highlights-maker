"""Concatenate processed clips into the final MP4.

Default: concat demuxer with stream copy (all clips share codec/resolution/fps
since they come from one recording session). Optional crossfade transitions
re-encode through an xfade/acrossfade filter chain.
"""

from __future__ import annotations

from pathlib import Path

from highlights.video.ffmpeg import probe_duration, run_ffmpeg

FADE_DURATION = 0.5


def concat_clips(ffmpeg_exe: Path, clips: list[Path], output_path: Path, work_dir: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if len(clips) == 1:
        run_ffmpeg(
            ffmpeg_exe,
            ["-y", "-i", str(clips[0]), "-c", "copy", "-movflags", "+faststart", str(output_path)],
        )
        return

    list_path = work_dir / "videos.txt"
    lines = []
    for clip in clips:
        escaped = str(clip).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines), encoding="utf-8")

    run_ffmpeg(
        ffmpeg_exe,
        [
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
    )


def concat_clips_with_fade(
    ffmpeg_exe: Path,
    clips: list[Path],
    output_path: Path,
    crf: int = 18,
) -> None:
    """Crossfade chain — re-encodes everything, noticeably slower than concat_clips."""
    if len(clips) == 1:
        concat_clips(ffmpeg_exe, clips, output_path, output_path.parent)
        return

    durations = [probe_duration(ffmpeg_exe, clip) for clip in clips]
    inputs: list[str] = []
    for clip in clips:
        inputs += ["-i", str(clip)]

    filters: list[str] = []
    video_in, audio_in = "[0:v]", "[0:a]"
    offset = 0.0
    for i in range(1, len(clips)):
        offset += durations[i - 1] - FADE_DURATION
        video_out = f"[v{i}]"
        audio_out = f"[a{i}]"
        filters.append(
            f"{video_in}[{i}:v]xfade=transition=fade:duration={FADE_DURATION}:offset={offset:.3f}{video_out}"
        )
        filters.append(f"{audio_in}[{i}:a]acrossfade=d={FADE_DURATION}{audio_out}")
        video_in, audio_in = video_out, audio_out

    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        ffmpeg_exe,
        [
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            video_in,
            "-map",
            audio_in,
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
    )
