"""ffprobe / ffmpeg access for the VOD: probing and sample-accurate audio cuts."""

from __future__ import annotations

import json
import os
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path

from highlights.broadcast.models import BroadcastSyncError
from highlights.video.ffmpeg import FfmpegError, ffprobe_command, run_ffmpeg

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_FORMAT = "pcm_s16le"
DURATION_WARN_SECONDS = 0.02  # exported WAV length vs requested before the sidecar notes it


@dataclass
class MediaInfo:
    path: Path
    duration: float
    audio_streams: list[dict] = field(default_factory=list)


def probe_media(ffmpeg_exe: Path, path: Path) -> MediaInfo:
    """Duration and audio streams of the VOD; raises with a readable message."""
    if not path.is_file():
        raise BroadcastSyncError(f"VOD not found: {path}")
    command = [
        ffprobe_command(ffmpeg_exe),
        "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,codec_name,sample_rate,channels,duration",
        "-of", "json",
        str(path),
    ]  # fmt: skip
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except FileNotFoundError:
        raise BroadcastSyncError("ffprobe not found; install FFmpeg or run `highlights update-tools`") from None
    if result.returncode != 0:
        raise BroadcastSyncError(f"ffprobe could not read {path}: {result.stderr.strip() or 'unknown error'}")
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise BroadcastSyncError(f"ffprobe returned unreadable output for {path}") from None

    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise BroadcastSyncError(f"{path.name} has no audio stream, so there is no commentary to cut")
    duration = _number(data.get("format", {}).get("duration"))
    if duration is None:
        duration = max((d for d in (_number(s.get("duration")) for s in audio) if d is not None), default=None)
    if duration is None or duration <= 0:
        raise BroadcastSyncError(f"Cannot determine the duration of {path.name}; the file may be truncated")
    return MediaInfo(path=path, duration=duration, audio_streams=audio)


def _number(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def extract_audio(ffmpeg_exe: Path, vod: Path, start: float, duration: float, out: Path, audio_stream: int = 0) -> None:
    """Cut [start, start+duration) of the VOD's audio to a 48 kHz stereo PCM WAV.

    The seek is before the input and the output is re-encoded, so ffmpeg trims to the
    exact sample instead of the previous keyframe. bitexact keeps repeated runs identical.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".part")
    try:
        run_ffmpeg(
            ffmpeg_exe,
            [
                "-y",
                "-ss", f"{start:.6f}",
                "-t", f"{duration:.6f}",
                "-i", str(vod),
                "-map", f"0:a:{audio_stream}",
                "-vn",
                "-af", f"aresample={SAMPLE_RATE}",
                "-ac", str(CHANNELS),
                "-c:a", SAMPLE_FORMAT,
                "-fflags", "+bitexact",
                "-flags:a", "+bitexact",
                "-f", "wav",
                str(partial),
            ],
        )  # fmt: skip
        os.replace(partial, out)
    except FfmpegError as exc:
        raise BroadcastSyncError(f"Audio cut failed for {out.name}: {exc}") from None
    finally:
        partial.unlink(missing_ok=True)


def wav_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as handle:
        return {
            "sample_rate": handle.getframerate(),
            "channels": handle.getnchannels(),
            "duration_seconds": handle.getnframes() / handle.getframerate(),
        }
