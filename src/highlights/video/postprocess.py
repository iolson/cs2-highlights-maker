"""Per-clip post-processing: attach game audio, optionally draw a text overlay."""

from __future__ import annotations

from pathlib import Path

from highlights.recording.recorder import SequenceOutput
from highlights.video.ffmpeg import run_ffmpeg

OVERLAY_FONT = "C\\:/Windows/Fonts/arialbd.ttf"


def _escape_drawtext(text: str) -> str:
    # drawtext treats \ : ' % specially; escape them for the filter string.
    for char, replacement in (("\\", "\\\\"), (":", "\\:"), ("'", "\\\\\\'"), ("%", "\\%")):
        text = text.replace(char, replacement)
    return text


def process_clip(
    ffmpeg_exe: Path,
    output: SequenceOutput,
    clips_dir: Path,
    overlay: bool,
) -> Path:
    """Produce clips_dir/clip_NN.mp4 with audio muxed in (stream copy when possible)."""
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clips_dir / f"clip_{output.sequence.number:02d}.mp4"

    map_args = ["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
    trailing_args: list[str] = []
    if output.audio_path is not None:
        # The game writes PCM wav; encode it to AAC while muxing.
        audio_args = ["-i", str(output.audio_path)]
    else:
        # Silent track keeps the stream layout identical across clips so the
        # final concat can stream-copy.
        audio_args = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        trailing_args = ["-shortest"]

    if overlay:
        label = _escape_drawtext(output.sequence.label)
        drawtext = (
            f"drawtext=fontfile='{OVERLAY_FONT}':text='{label}'"
            ":x=(w-tw)/2:y=h-140:fontsize=48:fontcolor=white"
            ":borderw=3:bordercolor=black:enable='between(t,0.5,4.5)'"
        )
        video_args = ["-vf", drawtext, "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p"]
    else:
        video_args = ["-c:v", "copy"]

    run_ffmpeg(
        ffmpeg_exe,
        [
            "-y",
            "-i",
            str(output.video_path),
            *audio_args,
            *map_args,
            *video_args,
            *trailing_args,
            str(clip_path),
        ],
    )
    return clip_path
