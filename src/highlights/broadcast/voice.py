"""Optional voice isolation: split the cut broadcast WAV into commentary and everything else.

Runs demucs (`pip install "cs2-highlights[voice]"`) as a subprocess of this Python on the
already-cut WAV, then converts its vocals stem back to the 48 kHz stereo PCM of the plain cut.
The voice stem is the same length and starts at the same sample, so every alignment in the
sidecar applies to it unchanged.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from highlights.broadcast.audio import CHANNELS, SAMPLE_FORMAT, SAMPLE_RATE
from highlights.broadcast.models import BroadcastSyncError
from highlights.video.ffmpeg import FfmpegError, run_ffmpeg

MODEL = "htdemucs"
DEVICES = ("auto", "cpu", "mps", "cuda")
INSTALL_HINT = 'Voice isolation needs demucs: pip install "cs2-highlights[voice]"'


def available() -> bool:
    return importlib.util.find_spec("demucs") is not None


def require() -> None:
    if not available():
        raise BroadcastSyncError(INSTALL_HINT)


def voice_path(wav: Path) -> Path:
    """`<name>-broadcast.wav` -> `<name>-broadcast-voice.wav`."""
    return wav.with_name(f"{wav.stem}-voice.wav")


def pick_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def isolate_voice(ffmpeg_exe: Path, wav: Path, out: Path, device: str = "auto") -> None:
    """Write the commentary-only version of `wav` to `out` (48 kHz stereo PCM, same length)."""
    require()
    device = pick_device(device)
    partial = out.with_name(out.name + ".part")
    with tempfile.TemporaryDirectory(prefix="demucs-") as work:
        command = [sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", MODEL, "-d", device, "-o", work, str(wav)]
        result = subprocess.run(command, capture_output=True, text=True)
        stem = Path(work) / MODEL / wav.stem / "vocals.wav"
        if result.returncode != 0 or not stem.is_file():
            detail = (result.stderr or result.stdout or "no output").strip().splitlines()
            raise BroadcastSyncError(f"Voice isolation failed for {wav.name} on {device}: {detail[-1] if detail else 'unknown error'}")
        try:
            run_ffmpeg(
                ffmpeg_exe,
                [
                    "-y",
                    "-i", str(stem),
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
            raise BroadcastSyncError(f"Converting the voice stem for {wav.name} failed: {exc}") from None
        finally:
            partial.unlink(missing_ok=True)
