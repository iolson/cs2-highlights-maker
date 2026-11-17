"""Write the broadcast WAV and its `-broadcast-sync.json` sidecar beside the highlight."""

from __future__ import annotations

import json
from pathlib import Path

from highlights.broadcast.audio import (
    CHANNELS,
    DURATION_WARN_SECONDS,
    SAMPLE_FORMAT,
    SAMPLE_RATE,
    extract_audio,
    wav_info,
)
from highlights.broadcast.models import BroadcastSyncError, ClipPlan
from highlights.broadcast.reports import LoadedReport

SIDECAR_SCHEMA = 1


def wav_path(output_dir: Path, plan: ClipPlan) -> Path:
    return output_dir / f"{plan.stem}-broadcast.wav"


def sidecar_path(output_dir: Path, plan: ClipPlan) -> Path:
    return output_dir / f"{plan.stem}-broadcast-sync.json"


def build_sidecar(
    plan: ClipPlan,
    report: LoadedReport,
    tickrate: float,
    map_name: str,
    demo_name: str,
    vod_name: str,
    anchor_file: str,
    mode: str,
    lead_in: float,
    lead_out: float,
    wav: dict | None,
) -> dict:
    """`wav` = measured info of the exported file (None when no audio was exported)."""
    seq = next(s for s in report.sequences if s.number == plan.sequence_number)
    window = plan.window
    clip = report.clip_path
    audio = None
    alignment = None
    if window is not None:
        audio = {
            "filename": None,
            "vod_start_seconds": round(window.vod_start, 3),
            "vod_end_seconds": round(window.vod_end, 3),
            "duration_seconds": round(window.duration, 3),
            "exported": wav is not None,
        }
        if wav is not None:
            audio.update(
                sample_rate=wav["sample_rate"],
                channels=wav["channels"],
                format=SAMPLE_FORMAT,
                measured_duration_seconds=round(wav["duration_seconds"], 3),
            )
        offset = plan.clip_start_vod - window.vod_start
        alignment = {
            "highlight_clip_start_demo_tick": plan.start_tick,
            "highlight_clip_start_vod_seconds": round(plan.clip_start_vod, 3),
            "audio_zero_vod_seconds": round(window.vod_start, 3),
            "audio_offset_at_highlight_start_seconds": round(offset, 3),
            "highlight_clip_end_audio_seconds": round(offset + (plan.end_tick - plan.start_tick) / tickrate, 3),
            "note": (
                f"Place the highlight start at +{offset:.3f}s of the exported audio on the nominal (unslowed) "
                "game timeline. The audio is a straight cut at natural speed; apply the rendered clip's own "
                "timing (slow-mo, kill-feed calibration) separately."
            ),
        }
    nominal = (plan.end_tick - plan.start_tick) / tickrate
    return {
        "schema_version": SIDECAR_SCHEMA,
        "status": plan.status,
        "confidence_basis": plan.basis,
        "mode": mode,
        "lead_in_seconds": lead_in,
        "lead_out_seconds": lead_out,
        "source": {
            "demo": demo_name,
            "vod": vod_name,
            "map": map_name,
            "tickrate": tickrate,
            "segment_id": plan.segment_id,
            "anchor_file": anchor_file,
        },
        "highlight": {
            "report": report.path.name,
            "clip": clip.name if clip else None,
            "round": plan.round,
            "sequence": plan.sequence_number,
            "start_tick": plan.start_tick,
            "end_tick": plan.end_tick,
            "kill_ticks": seq.kill_ticks,
        },
        "audio": audio,
        "alignment": alignment,
        "anchors_used": plan.anchors_used,
        "events": plan.events,
        "clamps": list(window.clamps) if window else [],
        "timing": {
            "nominal_clip_seconds": round(nominal, 3),
            "measured_clip_seconds": seq.clip_duration_seconds,
            "slowmo": seq.slowmo,
            "note": (
                "nominal = (end_tick - start_tick) / tickrate; measured is the ffprobe length from a "
                "finalized report (null when the clip was not rendered). The broadcast audio is never "
                "stretched to follow slow-mo."
            ),
        },
        "review": {"required": plan.status != "matched", "reasons": plan.reasons},
    }


def export_clip(
    ffmpeg_exe: Path | None,
    vod: Path | None,
    output_dir: Path,
    plan: ClipPlan,
    report: LoadedReport,
    allow_review: bool,
    audio_stream: int,
    **sidecar_args,
) -> dict:
    """Write the sidecar (always) and the WAV (matched, or needs_review with allow_review).

    Returns the sidecar dict; raises BroadcastSyncError when the cut fails.
    """
    wav = None
    exports = plan.window is not None and (plan.status == "matched" or (plan.status == "needs_review" and allow_review))
    notes: list[str] = []
    if exports:
        if ffmpeg_exe is None or vod is None:
            raise BroadcastSyncError("ffmpeg and the VOD are needed to export audio")
        target = wav_path(output_dir, plan)
        extract_audio(ffmpeg_exe, vod, plan.window.vod_start, plan.window.duration, target, audio_stream)
        wav = wav_info(target)
        if abs(wav["duration_seconds"] - plan.window.duration) > DURATION_WARN_SECONDS:
            notes.append(
                f"exported WAV is {wav['duration_seconds']:.3f}s, requested {plan.window.duration:.3f}s"
            )
        if wav["sample_rate"] != SAMPLE_RATE or wav["channels"] != CHANNELS:
            notes.append(f"unexpected WAV format {wav['sample_rate']} Hz / {wav['channels']} ch")
    data = build_sidecar(plan, report, wav=wav, **sidecar_args)
    if exports and data["audio"] is not None:
        data["audio"]["filename"] = wav_path(output_dir, plan).name
    data["diagnostics"] = notes
    sidecar = sidecar_path(output_dir, plan)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data
