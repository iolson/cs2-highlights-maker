"""Plan the broadcast audio window for every highlight sequence.

Matching today is anchor-driven: a clip is `matched` when a verified anchor vouches for
the timing nearby, `needs_review` when the only evidence is far away (a pause or replay
in between would shift the audio silently), and `unmatched` when nothing covers it.
There is no numeric confidence: the sidecar lists the evidence (`confidence_basis`).
"""

from __future__ import annotations

from pathlib import Path

from highlights.broadcast.models import ClipPlan, Window
from highlights.broadcast.reports import LoadedReport, ReportSequence
from highlights.broadcast.timeline import Timeline

MAX_ANCHOR_DISTANCE = 300.0  # seconds of game time a clip may be from its nearest anchor and still be `matched`
MODES = ("highlight", "round")


def plan_report(
    timeline: Timeline,
    report: LoadedReport,
    mode: str,
    lead_in: float,
    lead_out: float,
    vod_duration: float | None,
    max_anchor_distance: float = MAX_ANCHOR_DISTANCE,
) -> list[ClipPlan]:
    plans = []
    for seq in report.sequences:
        plan = plan_sequence(timeline, report, seq, mode, lead_in, lead_out, vod_duration, max_anchor_distance)
        plan.stem = report.path.stem if len(report.sequences) == 1 else f"{report.path.stem}-clip{seq.number:02d}"
        plans.append(plan)
    return plans


def plan_sequence(
    timeline: Timeline,
    report: LoadedReport,
    seq: ReportSequence,
    mode: str,
    lead_in: float,
    lead_out: float,
    vod_duration: float | None,
    max_anchor_distance: float = MAX_ANCHOR_DISTANCE,
) -> ClipPlan:
    rate = timeline.tickrate
    plan = ClipPlan(
        report=report.path,
        sequence_number=seq.number,
        round=seq.round,
        start_tick=seq.start_tick,
        end_tick=seq.end_tick,
        status="unmatched",
    )
    segment = timeline.segment_for_round(seq.round)
    if segment is None:
        plan.reasons.append(f"round {seq.round} is not covered by any segment in the anchor file")
        return plan
    plan.segment_id = segment.id
    anchors = timeline.usable_anchors(segment)
    if not anchors:
        plan.reasons.append(f"segment {segment.id!r} has no verified anchors")
        return plan

    base_start, base_end = seq.start_tick, seq.end_tick
    if mode == "round":
        round_ = timeline.demo.rounds[seq.round - 1]
        base_start, base_end = min(base_start, round_.start_tick), max(base_end, round_.end_tick)
    low, high = timeline.tick_range(segment)
    want_start, want_end = base_start - lead_in * rate, base_end + lead_out * rate
    tick_start, tick_end = max(want_start, low), min(want_end, high)
    clamps: list[str] = []
    if tick_start > want_start:
        clamps.append(
            f"lead-in cut to {(base_start - tick_start) / rate:.1f}s (of {lead_in:g}s): the live segment starts there"
        )
    if tick_end < want_end:
        clamps.append(
            f"lead-out cut to {(tick_end - base_end) / rate:.1f}s (of {lead_out:g}s): the live segment ends there"
        )

    vod_start, vod_end = timeline.to_vod(segment, tick_start), timeline.to_vod(segment, tick_end)
    clip_start, clip_end = timeline.to_vod(segment, seq.start_tick), timeline.to_vod(segment, seq.end_tick)
    if clip_start < 0 or (vod_duration is not None and clip_end > vod_duration):
        limit = f"the VOD is {vod_duration:.1f}s long" if vod_duration is not None else "the VOD starts at 0"
        plan.reasons.append(
            f"the clip maps to VOD {clip_start:.1f}s-{clip_end:.1f}s but {limit}; "
            "check the anchors or whether the VOD is cut short"
        )
        return plan
    if vod_start < 0:
        clamps.append(f"audio starts at the VOD start: lead-in cut by {-vod_start:.1f}s")
        vod_start = 0.0
    if vod_duration is not None and vod_end > vod_duration:
        clamps.append(f"audio ends at the VOD end: lead-out cut by {vod_end - vod_duration:.1f}s")
        vod_end = vod_duration
    plan.window = Window(tick_start, tick_end, vod_start, vod_end, tuple(clamps))
    plan.clip_start_vod = clip_start

    # Evidence: what vouches for the timing here.
    in_round = [a for a in anchors if timeline.round_of_tick(a.demo_tick) == seq.round]
    bracketed = len(anchors) >= 2 and anchors[0].demo_tick <= seq.start_tick and anchors[-1].demo_tick >= seq.end_tick

    def gap(anchor) -> float:
        if seq.start_tick <= anchor.demo_tick <= seq.end_tick:
            return 0.0
        return min(abs(anchor.demo_tick - seq.start_tick), abs(anchor.demo_tick - seq.end_tick)) / rate

    nearest = min(anchors, key=lambda a: (gap(a), a.demo_tick))
    distance = gap(nearest)
    if in_round:
        plan.basis.append("anchor_in_same_round")
    if bracketed:
        plan.basis.append("bracketed_by_consistent_anchors")
    plan.basis.append(f"{len(anchors)}_verified_anchors_consistent" if len(anchors) >= 2 else "single_anchor")
    plan.basis.append("tickrate_from_demo")

    used = {timeline.nearest_anchor(segment, t) for t in (tick_start, seq.start_tick, seq.end_tick, tick_end)}
    plan.anchors_used = [
        {
            "demo_tick": a.demo_tick,
            "vod_seconds": round(a.vod_seconds, 3),
            "event": a.event,
            "round": timeline.round_of_tick(a.demo_tick),
            "offset_seconds": round(timeline.offset(a), 3),
            "distance_seconds": round(gap(a), 1),
        }
        for a in sorted(used, key=lambda a: a.demo_tick)
        if a is not None
    ]

    if in_round or bracketed or distance <= max_anchor_distance:
        plan.status = "matched"
    else:
        plan.status = "needs_review"
        plan.reasons.append(
            f"the nearest anchor (round {timeline.round_of_tick(nearest.demo_tick)}) is {distance:.0f}s of game time "
            f"from this clip (limit {max_anchor_distance:g}s); a pause, replay or cut in between would shift the "
            f"audio. Add an anchor near round {seq.round} with `highlights broadcast-anchor`"
        )
    if not seq.kill_ticks and plan.status == "matched":
        plan.status = "needs_review"
        plan.reasons.append("the report lists no kill events for this clip, so there is nothing to verify the timing against")

    plan.events = [_event(e, timeline, segment, plan.window) for e in _kill_events(seq)]
    return plan


def _kill_events(seq: ReportSequence) -> list[dict]:
    if seq.kill_events:
        return seq.kill_events
    return [{"type": "kill", "tick": t} for t in seq.kill_ticks]


def _event(event: dict, timeline: Timeline, segment, window: Window) -> dict:
    tick = int(event["tick"])
    vod = timeline.to_vod(segment, tick)
    out = {
        "type": "kill",
        "demo_tick": tick,
        "vod_seconds": round(vod, 3),
        "audio_seconds": round(vod - window.vod_start, 3),
        "in_audio": window.vod_start <= vod <= window.vod_end,
    }
    for key in ("is_pov_kill", "is_selected_moment_kill", "weapon", "headshot"):
        if key in event:
            out[key] = event[key]
    victim = (event.get("victim") or {}).get("name")
    if victim:
        out["victim"] = victim
    return out
