"""Plan when a cinematic take can replace the POV footage of the same play.

Both takes cover the same ticks, so they line up frame for frame; the cinematic
is cut in during down time between kills, only while its camera is pointed at
the player. Visibility is a view-cone test (no map geometry, so walls between the
camera and the player are not detected) and the whole plan is computed from the
demo and the cam path, before anything is recorded.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from math import atan, atan2, degrees, dist, radians, tan

from highlights.recording.campaths import CamPath, pose_at

SAMPLE_STEP = 0.1  # seconds between visibility checks
KILL_PAD_BEFORE = 1.5  # POV is kept this long before a kill ...
KILL_PAD_AFTER = 1.5  # ... and after it
MIN_SEGMENT = 1.5  # shorter cinematic cuts are dropped
MERGE_GAP = 0.5  # POV blips shorter than this inside a cinematic cut are absorbed
VIEW_MARGIN = 0.85  # fraction of the half-FOV the player must sit inside
ASPECT = 16 / 9
MAX_DISTANCE = 4000.0  # world units; farther than this the player is a speck

Point = tuple[float, float, float]
Trajectory = list[tuple[float, Point]]  # (seconds from clip start, position), ascending


def position_at(samples: Trajectory, t: float) -> Point | None:
    if not samples or t < samples[0][0] or t > samples[-1][0]:
        return None
    index = bisect_right([s[0] for s in samples], t) - 1
    if index >= len(samples) - 1:
        return samples[-1][1]
    (t0, a), (t1, b) = samples[index], samples[index + 1]
    w = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
    return tuple(x + (y - x) * w for x, y in zip(a, b))  # type: ignore[return-value]


def _in_view(position: Point, forward: Point, fov: float, target: Point) -> bool:
    d = (target[0] - position[0], target[1] - position[1], target[2] - position[2])
    distance = dist(position, target)
    if not 50 < distance <= MAX_DISTANCE:
        return False
    # Camera basis without roll: left = up_world x forward, up = forward x left.
    left = (-forward[1], forward[0], 0.0)
    norm = (left[0] ** 2 + left[1] ** 2) ** 0.5
    if norm < 1e-6:  # looking straight up/down
        return False
    left = (left[0] / norm, left[1] / norm, 0.0)
    up = (
        forward[1] * left[2] - forward[2] * left[1],
        forward[2] * left[0] - forward[0] * left[2],
        forward[0] * left[1] - forward[1] * left[0],
    )
    dot = lambda u, v: u[0] * v[0] + u[1] * v[1] + u[2] * v[2]  # noqa: E731
    ahead = dot(d, forward)
    if ahead <= 0:
        return False
    half_h = radians(fov) / 2
    half_v = atan(tan(half_h) / ASPECT)  # fov is the horizontal angle
    yaw_off = abs(atan2(dot(d, left), ahead))
    pitch_off = abs(atan2(dot(d, up), ahead))
    return yaw_off <= half_h * VIEW_MARGIN and pitch_off <= half_v * VIEW_MARGIN


def plan_splice(
    duration: float,
    path: CamPath,
    kill_times: list[float],
    target_at: Callable[[float], Point | None],
) -> list[tuple[float, float]]:
    """Segments (start, end) in seconds from clip start where the cinematic replaces the POV.

    kill_times are the POV player's kills (clip seconds); target_at gives the
    player's position at a time (None when unknown, which counts as not visible).
    """
    protected = [(k - KILL_PAD_BEFORE, k + KILL_PAD_AFTER) for k in kill_times]
    steps = int(duration / SAMPLE_STEP) + 1
    visible: list[bool] = []
    for i in range(steps):
        t = i * SAMPLE_STEP
        pose = pose_at(path, t)
        target = target_at(t)
        ok = (
            pose is not None
            and target is not None
            and not any(a <= t <= b for a, b in protected)
            and _in_view(pose[0], pose[1], pose[2], target)
        )
        visible.append(ok)

    runs: list[list[float]] = []
    for i, ok in enumerate(visible):
        if not ok:
            continue
        t = i * SAMPLE_STEP
        if runs and t - runs[-1][1] <= MERGE_GAP + SAMPLE_STEP + 1e-9:
            runs[-1][1] = t
        else:
            runs.append([t, t])
    # A run of one sample spans one step of footage.
    return [
        (round(a, 2), round(min(b + SAMPLE_STEP, duration), 2))
        for a, b in runs
        if b + SAMPLE_STEP - a >= MIN_SEGMENT - 1e-9
    ]


def coverage(segments: list[tuple[float, float]]) -> float:
    return sum(b - a for a, b in segments)
