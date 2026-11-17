"""Anchor file: load, validate, and add anchors (`highlights broadcast-anchor`).

An anchor says "demo tick T is at VOD time S". Anchors live in segments of
continuous live play; nothing is ever interpolated across segments.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from highlights.broadcast.models import Anchor, AnchorFile, BroadcastSyncError, Segment

SCHEMA_VERSION = 1
KINDS = ("live",)  # replays, desk segments and breaks are simply not covered by any segment
_TIMESTAMP = re.compile(r"^(?:(?:(\d+):)?(\d+):)?(\d+(?:\.\d+)?)$")


def parse_timestamp(text: str) -> float:
    """'1:02:03.5', '12:03' or '723.4' -> seconds."""
    match = _TIMESTAMP.match(text.strip())
    if not match:
        raise BroadcastSyncError(f"Cannot read VOD time {text!r}; use HH:MM:SS.mmm, MM:SS or plain seconds")
    hours, minutes, seconds = match.groups()
    if hours is not None and minutes is not None and int(minutes) >= 60:
        raise BroadcastSyncError(f"Bad VOD time {text!r}: minutes must be below 60 when hours are given")
    total = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds)
    if minutes is not None and float(seconds) >= 60:
        raise BroadcastSyncError(f"Bad VOD time {text!r}: seconds must be below 60 when minutes are given")
    return total


def format_timestamp(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    millis = round(abs(seconds) * 1000)
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    return f"{sign}{hours}:{minutes:02d}:{rest // 1000:02d}.{rest % 1000:03d}"


def _anchor_from_dict(raw: object, where: str) -> Anchor:
    if not isinstance(raw, dict):
        raise BroadcastSyncError(f"{where}: an anchor must be an object")
    tick, vod = raw.get("demo_tick"), raw.get("vod_seconds")
    if isinstance(tick, bool) or not isinstance(tick, int) or tick < 0:
        raise BroadcastSyncError(f"{where}: demo_tick must be a non-negative integer, got {tick!r}")
    if isinstance(vod, bool) or not isinstance(vod, (int, float)) or vod < 0:
        raise BroadcastSyncError(f"{where}: vod_seconds must be a non-negative number, got {vod!r}")
    return Anchor(
        demo_tick=tick,
        vod_seconds=float(vod),
        event=str(raw.get("event", "manual")),
        source=str(raw.get("source", "manual")),
        verified=raw.get("verified", True) is True,
    )


def load_anchor_file(path: Path) -> AnchorFile:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BroadcastSyncError(f"Anchor file not found: {path}") from None
    except (OSError, ValueError) as exc:
        raise BroadcastSyncError(f"Cannot read anchor file {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise BroadcastSyncError(f"{path}: expected a JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise BroadcastSyncError(f"{path}: schema_version must be {SCHEMA_VERSION}, got {raw.get('schema_version')!r}")
    if not isinstance(raw.get("demo"), str) or not raw["demo"]:
        raise BroadcastSyncError(f"{path}: 'demo' (the .dem file name) is required")
    segments_raw = raw.get("segments")
    if not isinstance(segments_raw, list) or not segments_raw:
        raise BroadcastSyncError(f"{path}: 'segments' must be a non-empty list")

    segments: list[Segment] = []
    for number, item in enumerate(segments_raw, 1):
        if not isinstance(item, dict):
            raise BroadcastSyncError(f"{path}: segment {number} must be an object")
        seg_id = item.get("id")
        if not isinstance(seg_id, str) or not seg_id:
            raise BroadcastSyncError(f"{path}: segment {number} needs a string 'id'")
        where = f"{path}: segment {seg_id!r}"
        kind = item.get("kind", "live")
        if kind not in KINDS:
            raise BroadcastSyncError(f"{where}: kind {kind!r} is not supported (only {', '.join(KINDS)})")
        bounds = []
        for key in ("round_start", "round_end"):
            value = item.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise BroadcastSyncError(f"{where}: {key} must be a round number >= 1, got {value!r}")
            bounds.append(value)
        anchors_raw = item.get("anchors")
        if not isinstance(anchors_raw, list):
            raise BroadcastSyncError(f"{where}: 'anchors' must be a list")
        anchors = tuple(
            sorted(
                (_anchor_from_dict(a, f"{where} anchor {i}") for i, a in enumerate(anchors_raw, 1)),
                key=lambda a: a.demo_tick,
            )
        )
        segments.append(Segment(seg_id, kind, bounds[0], bounds[1], anchors))
    return AnchorFile(
        path=path,
        match_id=raw.get("match_id"),
        demo=raw["demo"],
        vod=raw.get("vod"),
        segments=segments,
    )


def save_anchor_file(anchors: AnchorFile) -> None:
    data: dict = {"schema_version": SCHEMA_VERSION}
    if anchors.match_id:
        data["match_id"] = anchors.match_id
    data["demo"] = anchors.demo
    if anchors.vod:
        data["vod"] = anchors.vod
    data["segments"] = [
        {
            "id": s.id,
            "kind": s.kind,
            **({"round_start": s.round_start} if s.round_start is not None else {}),
            **({"round_end": s.round_end} if s.round_end is not None else {}),
            "anchors": [
                {
                    "demo_tick": a.demo_tick,
                    "vod_seconds": round(a.vod_seconds, 3),
                    "event": a.event,
                    "source": a.source,
                    "verified": a.verified,
                }
                for a in s.anchors
            ],
        }
        for s in anchors.segments
    ]
    anchors.path.parent.mkdir(parents=True, exist_ok=True)
    anchors.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


EVENTS = ("round_start", "freeze_end", "round_end", "kill")


def event_tick(demo, round_number: int, event: str, kill: int | None = None) -> int:
    """Demo tick of a round event, so the user never has to look ticks up."""
    if not 1 <= round_number <= len(demo.rounds):
        raise BroadcastSyncError(f"Round {round_number} does not exist: the demo has {len(demo.rounds)} rounds")
    round_ = demo.rounds[round_number - 1]
    if event == "kill":
        kills = sorted((k for k in demo.kills if k.round_index == round_number - 1), key=lambda k: k.tick)
        if kill is None:
            raise BroadcastSyncError("--event kill needs --kill N (the Nth kill of the round, counting from 1)")
        if not 1 <= kill <= len(kills):
            raise BroadcastSyncError(f"Round {round_number} has {len(kills)} kills; --kill {kill} does not exist")
        return kills[kill - 1].tick
    if kill is not None:
        raise BroadcastSyncError("--kill only goes with --event kill")
    return {"round_start": round_.start_tick, "freeze_end": round_.freeze_end_tick, "round_end": round_.end_tick}[event]


def add_anchor(
    anchors: AnchorFile,
    demo,
    segment_id: str,
    anchor: Anchor,
    rounds: tuple[int, int] | None = None,
    drift_tolerance: float | None = None,
) -> AnchorFile:
    """Return `anchors` with the anchor added; the whole file is validated first, so a
    conflicting anchor raises and nothing is written."""
    from highlights.broadcast.timeline import DRIFT_TOLERANCE, Timeline

    segments = list(anchors.segments)
    index = next((i for i, s in enumerate(segments) if s.id == segment_id), None)
    if index is None:
        first, last = rounds if rounds else (None, None)
        segments.append(Segment(segment_id, "live", first, last, (anchor,)))
    else:
        existing = segments[index]
        if rounds and (existing.round_start, existing.round_end) != rounds:
            raise BroadcastSyncError(
                f"Segment {segment_id!r} already covers rounds {existing.round_start}-{existing.round_end}; "
                "edit the anchor file to change that"
            )
        if any(a.demo_tick == anchor.demo_tick for a in existing.anchors):
            raise BroadcastSyncError(
                f"Segment {segment_id!r} already has an anchor at tick {anchor.demo_tick}; edit the file to change it"
            )
        merged = tuple(sorted((*existing.anchors, anchor), key=lambda a: a.demo_tick))
        segments[index] = Segment(existing.id, existing.kind, existing.round_start, existing.round_end, merged)
    updated = AnchorFile(anchors.path, anchors.match_id, anchors.demo, anchors.vod, segments)
    Timeline(demo, updated, DRIFT_TOLERANCE if drift_tolerance is None else drift_tolerance)
    return updated
