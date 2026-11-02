"""Turn POV sequences into POV + cinematic sequences (--cinematic)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from rich.markup import escape

from highlights.models import Sequence
from highlights.places import PlaceMap
from highlights.recording.campaths import CamPath, path_distance, rank_campaths
from highlights.recording.path_routes import path_route
from highlights.recording.splice_plan import MIN_SEGMENT, Point, Trajectory, coverage, plan_splice, position_at
from highlights.util.log import info, warn

MIN_COVERAGE = MIN_SEGMENT  # seconds a path must cut in for to be used in splice mode: at least one cut
CANDIDATES_SHOWN = 3


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


def _describe(candidates: list[dict]) -> str:
    parts = []
    for c in candidates:
        cover = "" if c["coverage_seconds"] is None else f", sees player {c['coverage_seconds']:.1f}s"
        label = f' "{c["label"]}"' if c.get("label") else ""
        route = f" [{' > '.join(c['route'])}]" if c.get("route") else ""
        parts.append(f"{c['name']}{label} ({c['distance']:.0f}u{cover}){route}")
    return ", ".join(parts)


def add_cinematic_sequences(
    sequences: list[Sequence],
    campaths: list[CamPath],
    spatial_by_sequence: dict[int, dict],
    tickrate: float,
    takes: int = 1,
    splice: bool = False,
    forced: CamPath | None = None,
    place_map: PlaceMap | None = None,
    labels: dict[str, str] | None = None,
    quiet: bool = False,
) -> tuple[list[Sequence], dict[int, dict]]:
    """Follow each POV sequence with cinematic re-recordings of the same ticks.

    Paths are ranked by how close they pass to the play's kills. Append mode takes
    the `takes` closest. Splice mode takes the closest path that can cut in for at
    least MIN_COVERAGE seconds of down time (its plan is stored in
    `splice_segments`); plays no path can cover are left POV-only. `forced` skips
    the ranking and uses that path for every play (splice mode still needs it to
    cover something). `quiet` suppresses all of its console output.

    Returns the interleaved list renumbered 1..N and the spatial metadata re-keyed
    to the new numbers.
    """
    result: list[Sequence] = []
    spatial: dict[int, dict] = {}
    routes: dict[str, list[str]] = {}

    def say(kind: str, message: str) -> None:
        if quiet:
            return
        (info if kind == "info" else warn)(message)

    if splice and takes > 1:
        say("info", "--cinematic-takes is ignored with --cinematic-mode splice (one cinematic per play)")

    def append(sequence: Sequence, source_number: int, **changes: object) -> None:
        number = len(result) + 1
        result.append(replace(sequence, number=number, **changes))
        if source_number in spatial_by_sequence:
            spatial[number] = spatial_by_sequence[source_number]

    for sequence in sequences:
        append(sequence, sequence.number)
        pov_number = len(result)
        source = spatial_by_sequence.get(sequence.number)
        targets = _action_targets(source)
        ranked = [forced] if forced else rank_campaths(campaths, targets)
        if not ranked or not targets:
            say("warn", f"{sequence.label}: no spatial data to match cam paths, no cinematic take")
            continue
        distance = {path.name: path_distance(path, targets) for path in ranked}
        play_route = (source or {}).get("route", {}).get("text")
        route_note = f" [route: {play_route}]" if play_route else ""

        def candidate(path: CamPath, cover: float | None) -> dict:
            route = routes.get(path.name)
            if route is None:
                route = path_route(path, place_map) if place_map else []
                routes[path.name] = route
            return {
                "name": path.name,
                "distance": round(distance[path.name], 1),
                "coverage_seconds": cover,
                "route": route,
                "label": (labels or {}).get(path.name.lower()),
            }

        if not splice:
            shown = [candidate(path, None) for path in ranked[: max(takes, CANDIDATES_SHOWN)]]
            say("info", escape(f"{sequence.label}{route_note}: cinematic candidates — {_describe(shown)}"))
            for path in ranked[:takes]:
                if path.duration < sequence.duration_seconds(tickrate):
                    say(
                        "warn",
                        f"{sequence.label}: cam path {path.name} runs {path.duration:.0f}s, shorter "
                        "than the clip; the POV view returns when it ends",
                    )
                append(
                    sequence,
                    sequence.number,
                    label=f"{sequence.label} — cinematic {path.name}",
                    campath=path.file,
                    cinematic_of=pov_number,
                    cinematic_candidates=shown,
                )
            continue

        target_at = _target_at(sequence, source or {}, tickrate)
        kills = _kill_times(sequence, tickrate)
        duration = sequence.duration_seconds(tickrate)
        plans = [(path, plan_splice(duration, path, kills, target_at)) for path in ranked]
        threshold = 1e-9 if forced else MIN_COVERAGE
        chosen = next(((p, segs) for p, segs in plans if coverage(segs) >= threshold), None)
        shown_plans = plans[:CANDIDATES_SHOWN]
        if chosen and chosen not in shown_plans:
            shown_plans = [*shown_plans, chosen]
        shown = [candidate(p, round(coverage(segs), 1)) for p, segs in shown_plans]
        if chosen is None:
            say(
                "warn",
                f"{sequence.label}: no cam path sees the player for {MIN_COVERAGE:g}s of down time, "
                f"POV only — closest were {_describe(shown)}",
            )
            continue
        path, segments = chosen
        say("info", escape(f"{sequence.label}{route_note}: cinematic {path.name} — candidates: {_describe(shown)}"))
        append(
            sequence,
            sequence.number,
            label=f"{sequence.label} — cinematic {path.name}",
            campath=path.file,
            cinematic_of=pov_number,
            splice_segments=segments,
            cinematic_candidates=shown,
        )
    return result, spatial
