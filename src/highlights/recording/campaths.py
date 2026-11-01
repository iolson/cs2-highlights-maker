"""User-supplied HLAE cam paths for --cinematic recordings.

The paths themselves are never part of this repository: they are read from a
gitignored folder (default <repo>/campaths, one subfolder per map) at run time.
Files are HLAE campath XML (`<campath><points><p t x y z .../>`); `.dz`,
`.campath` and `.xml` are accepted.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from math import cos, dist, radians, sin, sqrt
from pathlib import Path

from highlights.config import Config
from highlights.util.log import warn

CAMPATH_SUFFIXES = {".dz", ".campath", ".xml"}


@dataclass(frozen=True)
class Keyframe:
    t: float  # seconds since the first point
    pos: tuple[float, float, float]  # x, y, z in HLAE campath space
    forward: tuple[float, float, float]  # unit view direction (roll is ignored)
    fov: float  # degrees, taken as horizontal


@dataclass(frozen=True)
class CamPath:
    name: str  # file stem, e.g. "A4"
    file: Path
    keyframes: tuple[Keyframe, ...]
    duration: float  # seconds between the first and last point

    @property
    def points(self) -> tuple[tuple[float, float, float], ...]:
        return tuple(k.pos for k in self.keyframes)


def default_campaths_dir() -> Path:
    # src/highlights/recording/campaths.py -> repo root
    return Path(__file__).resolve().parents[3] / "campaths"


def resolve_campaths_dir(config: Config) -> Path:
    if config.paths.campaths_dir != "auto":
        return Path(config.paths.campaths_dir).expanduser()
    return default_campaths_dir()


def _normalize(v: tuple[float, float, float]) -> tuple[float, float, float]:
    length = sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2) or 1.0
    return (v[0] / length, v[1] / length, v[2] / length)


def _forward(attrib: dict[str, str]) -> tuple[float, float, float]:
    """View direction of a point. Quaternion wins over Euler angles when both are
    present (HLAE's rule). Quake space: x forward, y left, z up; positive pitch
    looks down, positive yaw turns left."""
    if all(key in attrib for key in ("qw", "qx", "qy", "qz")):
        w, x, y, z = (float(attrib[key]) for key in ("qw", "qx", "qy", "qz"))
        return _normalize((1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y)))
    pitch, yaw = radians(float(attrib["ry"])), radians(float(attrib["rz"]))
    return (cos(pitch) * cos(yaw), cos(pitch) * sin(yaw), -sin(pitch))


def parse_campath(file: Path) -> CamPath | None:
    """Read a campath file; None when it has fewer than two usable points."""
    try:
        root = ET.parse(file).getroot()
    except (ET.ParseError, OSError) as exc:
        warn(f"Skipping cam path {file.name}: {exc}")
        return None
    raw: list[Keyframe] = []
    for p in root.iter("p"):
        try:
            raw.append(
                Keyframe(
                    t=float(p.attrib["t"]),
                    pos=(float(p.attrib["x"]), float(p.attrib["y"]), float(p.attrib["z"])),
                    forward=_forward(p.attrib),
                    fov=float(p.attrib.get("fov", 90)),
                )
            )
        except (KeyError, ValueError):
            continue
    if len(raw) < 2:
        warn(f"Skipping cam path {file.name}: fewer than two points")
        return None
    raw.sort(key=lambda k: k.t)
    start = raw[0].t
    keyframes = tuple(Keyframe(k.t - start, k.pos, k.forward, k.fov) for k in raw)
    return CamPath(name=file.stem, file=file.resolve(), keyframes=keyframes, duration=keyframes[-1].t)


def pose_at(path: CamPath, t: float) -> tuple[tuple[float, float, float], tuple[float, float, float], float] | None:
    """(position, forward, fov) of the camera t seconds after the path starts, or
    None outside the path. Linear interpolation (HLAE's own curve is smoother)."""
    keys = path.keyframes
    if t < 0 or t > path.duration:
        return None
    for a, b in zip(keys, keys[1:]):
        if a.t <= t <= b.t:
            w = 0.0 if b.t == a.t else (t - a.t) / (b.t - a.t)
            lerp = lambda u, v: tuple(x + (y - x) * w for x, y in zip(u, v))  # noqa: E731
            return lerp(a.pos, b.pos), _normalize(lerp(a.forward, b.forward)), a.fov + (b.fov - a.fov) * w
    return keys[-1].pos, keys[-1].forward, keys[-1].fov


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
