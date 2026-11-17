"""Thin subprocess wrapper around ffmpeg/ffprobe with readable errors."""

from __future__ import annotations

import subprocess
from pathlib import Path


class FfmpegError(RuntimeError):
    pass


def run_ffmpeg(ffmpeg_exe: Path, args: list[str]) -> None:
    cmd = [str(ffmpeg_exe), "-hide_banner", "-loglevel", "error", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-10:]
        raise FfmpegError(
            "ffmpeg failed (exit {}):\n  {}\ncommand: {}".format(
                result.returncode, "\n  ".join(tail), subprocess.list2cmdline(cmd)
            )
        )


def ffprobe_command(ffmpeg_exe: Path) -> str:
    """ffprobe next to ffmpeg (ffprobe.exe on Windows, ffprobe elsewhere), else the one on PATH."""
    for name in ("ffprobe.exe", "ffprobe"):
        sibling = ffmpeg_exe.parent / name
        if sibling.is_file():
            return str(sibling)
    return "ffprobe"


def probe_duration(ffmpeg_exe: Path, media_path: Path) -> float:
    """Duration in seconds via ffprobe (assumed to sit next to ffmpeg.exe)."""
    probe_cmd = ffprobe_command(ffmpeg_exe)
    result = subprocess.run(
        [
            probe_cmd,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise FfmpegError(f"ffprobe failed for {media_path}: {result.stderr}")
    return float(result.stdout.strip())
