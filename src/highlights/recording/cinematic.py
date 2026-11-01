"""Turn POV sequences into POV + cinematic sequences (--cinematic)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from highlights.models import Sequence
from highlights.recording.campaths import CamPath, rank_campaths
from highlights.recording.splice_plan import Point, Trajectory, coverage, plan_splice, position_at
from highlights.util.log import info, warn


def _action_targets(spatial: dict | None) -> list[Point]:
    """World positions to frame: the selected kills, else the primary action center."""
    if not spatial:
        return []
    centers = [
        e["engagement_center"]
        for e in spatial.get("engagements", [])
        if e.get("is_selected_moment_kill") and e.get("engagement_center")
    ]
    if not centers and spatial.get("primary_action_center_xyz"):
        centers = [spatial["primary_action_center_xyz"]]
    return [(c["x"], c["y"], c["z"]) for c in centers]


def _trajectories(spatial: dict) -> dict[str, Trajectory]:
    result: dict[str, Trajectory] = {}
    for track in spatial.get("pov_trajectories", []):
        result[track["steamid"]] = [
            (s["seconds_from_clip_start"], (s["xyz"]["x"], s["xyz"]["y"], s["xyz"]["z"]))
            for s in track["samples"]
        ]
    return result


def _target_at(sequence: Sequence, spatial: dict, tickrate: float) -> Callable[[float], Point | None]:
    """Position of whoever the POV camera is on at a given clip time."""
    tracks = _trajectories(spatial)
    cameras = sorted(sequence.cameras, key=lambda c: c.tick)

    def target(t: float) -> Point | None:
        tick = sequence.start_tick + t * tickrate
        active = next((c.steamid for c in reversed(cameras) if c.tick <= tick), None)
        return position_at(tracks.get(active, []), t) if active else None

    return target


def _kill_times(sequence: Sequence, tickrate: float) -> list[float]:
    ticks = {k.tick for m in sequence.moments for k in m.kills}
    return sorted((t - sequence.start_tick) / tickrate for t in ticks if sequence.start_tick <= t <= sequence.end_tick)


def add_cinematic_sequences(
    sequences: list[Sequence],
    campaths: list[CamPath],
    spatial_by_sequence: dict[int, dict],
    tickrate: float,
    takes: int = 1,
    splice: bool = False,
) -> tuple[list[Sequence], dict[int, dict]]:
    """Follow each POV sequence with cinematic re-recordings of the same ticks.

    Append mode: `takes` best-matching paths per play, each a clip of its own.
    Splice mode: the single path that can cut in for the longest stretch of down
    time; its plan is stored in `splice_segments`, and plays no path can cover are
    left POV-only (no point recording them).

    Returns the interleaved list renumbered 1..N and the spatial metadata re-keyed
    to the new numbers.
    """
    result: list[Sequence] = []
    spatial: dict[int, dict] = {}
    if splice and takes > 1:
        info("--cinematic-takes is ignored with --cinematic-mode splice (one cinematic per play)")

    def append(sequence: Sequence, source_number: int, **changes: object) -> None:
        number = len(result) + 1
        result.append(replace(sequence, number=number, **changes))
        if source_number in spatial_by_sequence:
            spatial[number] = spatial_by_sequence[source_number]

    for sequence in sequences:
        append(sequence, sequence.number)
        pov_number = len(result)
        source = spatial_by_sequence.get(sequence.number)
        ranked = rank_campaths(campaths, _action_targets(source))
        if not ranked:
            warn(f"{sequence.label}: no spatial data to match cam paths, no cinematic take")
            continue

        if not splice:
            for path in ranked[:takes]:
                if path.duration < sequence.duration_seconds(tickrate):
                    warn(
                        f"{sequence.label}: cam path {path.name} runs {path.duration:.0f}s, shorter "
                        "than the clip; the POV view returns when it ends"
                    )
                append(
                    sequence,
                    sequence.number,
                    label=f"{sequence.label} — cinematic {path.name}",
                    campath=path.file,
                    cinematic_of=pov_number,
                )
            continue

        target_at = _target_at(sequence, source or {}, tickrate)
        kills = _kill_times(sequence, tickrate)
        duration = sequence.duration_seconds(tickrate)
        plans = [(path, plan_splice(duration, path, kills, target_at)) for path in ranked]
        # Longest coverage wins; proximity (the ranking order) breaks ties.
        path, segments = max(plans, key=lambda plan: coverage(plan[1]))
        if not segments:
            warn(f"{sequence.label}: no cam path sees the player during down time, POV only")
            continue
        append(
            sequence,
            sequence.number,
            label=f"{sequence.label} — cinematic {path.name}",
            campath=path.file,
            cinematic_of=pov_number,
            splice_segments=segments,
        )
    return result, spatial
