"""User-supplied HLAE cam paths for --cinematic recordings.

The paths themselves are never part of this repository: they are read from a
gitignored folder (default <repo>/campaths, one subfolder per map) at run time.
Files are HLAE campath XML (`<campath><points><p t x y z .../>`); `.dz`,
`.campath` and `.xml` are accepted.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from math import dist
from pathlib import Path

from highlights.config import Config
from highlights.models import Sequence
from highlights.util.log import warn

CAMPATH_SUFFIXES = {".dz", ".campath", ".xml"}


@dataclass(frozen=True)
class CamPath:
    name: str  # file stem, e.g. "A4"
    file: Path
    points: tuple[tuple[float, float, float], ...]  # x, y, z in HLAE campath space
    duration: float  # seconds between the first and last point


def default_campaths_dir() -> Path:
    # src/highlights/recording/campaths.py -> repo root
    return Path(__file__).resolve().parents[3] / "campaths"


def resolve_campaths_dir(config: Config) -> Path:
    if config.paths.campaths_dir != "auto":
        return Path(config.paths.campaths_dir).expanduser()
    return default_campaths_dir()


def parse_campath(file: Path) -> CamPath | None:
    """Read a campath file; None when it has fewer than two usable points."""
    try:
        root = ET.parse(file).getroot()
    except (ET.ParseError, OSError) as exc:
        warn(f"Skipping cam path {file.name}: {exc}")
        return None
    times: list[float] = []
    points: list[tuple[float, float, float]] = []
    for p in root.iter("p"):
        try:
            times.append(float(p.attrib["t"]))
            points.append((float(p.attrib["x"]), float(p.attrib["y"]), float(p.attrib["z"])))
        except (KeyError, ValueError):
            continue
    if len(points) < 2:
        warn(f"Skipping cam path {file.name}: fewer than two points")
        return None
    return CamPath(name=file.stem, file=file.resolve(), points=tuple(points), duration=max(times) - min(times))


def load_campaths(base: Path, map_name: str) -> list[CamPath]:
    """Cam paths for a map, from the subfolder named like it ("Nuke" or "de_nuke")."""
    if not base.is_dir():
        return []
    wanted = {map_name.lower(), map_name.lower().removeprefix("de_")}
    paths: list[CamPath] = []
    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or folder.name.lower() not in wanted:
            continue
        for file in sorted(folder.iterdir()):
            if file.suffix.lower() in CAMPATH_SUFFIXES and (parsed := parse_campath(file)):
                paths.append(parsed)
    return paths


def rank_campaths(campaths: list[CamPath], targets: list[tuple[float, float, float]]) -> list[CamPath]:
    """Order paths by how close they pass to the action (mean over targets of the
    nearest path point). Assumes campath space equals demo world space — not yet
    validated in-game."""
    if not targets:
        return []

    def score(path: CamPath) -> float:
        return sum(min(dist(point, target) for point in path.points) for target in targets) / len(targets)

    return sorted(campaths, key=lambda path: (score(path), path.name))


def _action_targets(spatial: dict | None) -> list[tuple[float, float, float]]:
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


def add_cinematic_sequences(
    sequences: list[Sequence],
    campaths: list[CamPath],
    spatial_by_sequence: dict[int, dict],
    tickrate: float,
    takes: int = 1,
) -> tuple[list[Sequence], dict[int, dict]]:
    """Follow each POV sequence with `takes` cinematic re-recordings of the same ticks.

    Returns the interleaved list renumbered 1..N (so final clips stack POV, then
    its cinematics) and the spatial metadata re-keyed to the new numbers.
    """
    result: list[Sequence] = []
    spatial: dict[int, dict] = {}

    def append(sequence: Sequence, source_number: int, **changes: object) -> None:
        number = len(result) + 1
        result.append(replace(sequence, number=number, **changes))
        if source_number in spatial_by_sequence:
            spatial[number] = spatial_by_sequence[source_number]

    for sequence in sequences:
        append(sequence, sequence.number)
        pov_number = len(result)
        ranked = rank_campaths(campaths, _action_targets(spatial_by_sequence.get(sequence.number)))
        if not ranked:
            warn(f"{sequence.label}: no spatial data to match cam paths, no cinematic take")
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
    return result, spatial
