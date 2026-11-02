"""Generated cam paths that follow the POV player (--intro-cam, --downtime-cam).

Instead of a user-supplied cam path, the path is computed from the demo: a pose
relative to the player's eye (face, profile, gun, third-person, helicopter) that
eases into the exact first-person eye pose at the end of an opener, or out of and
back into it during down time, so the cut back to the POV take has no jump.
Everything here is pure geometry on clip-second tracks; the demo is only read by
`spatial.sample_pov_tracks`. Offsets are estimates from reference clips and are
meant to be tuned (the constants below); nothing is checked against map geometry,
so a camera can end up inside a wall.

Angles are degrees, CS2 convention: positive yaw turns toward +y, positive pitch
looks down (same as `campaths.parse_campath`).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from math import atan2, cos, degrees, hypot, radians, sin
from pathlib import Path

from highlights.recording.splice_plan import KILL_PAD, MIN_SEGMENT, _protected_windows

STYLES = ("face", "profile", "gun", "tps", "heli")

EYE_HEIGHT = 64.0  # standing eye height above the player's origin (feet)
POV_FOV = 90.0  # fov of the first-person view; assumed, not validated in-game
KEY_STEP = 1 / 16  # seconds between generated keyframes
OPENER_SECONDS = 3.5  # opener length (shortened to stay clear of the first kill)
OPENER_HOLD = 0.55  # opener: fraction spent on the style shot before easing into the eye
DOWNTIME_MAX = 4.0  # longest stretch of one down-time cut
DOWNTIME_OUT = 0.3  # down time: fraction easing out of the eye ...
DOWNTIME_BACK = 0.3  # ... and back into it
POV_GAP = 1.0  # plain POV kept between the opener and the first down-time cut
TRACK_SLACK = 0.25  # a track may end this much before the clip does; its last pose is held

Vec = tuple[float, float, float]
# (clip seconds, player origin x/y/z, yaw, pitch) of the camera player
Track = list[tuple[float, Vec, float, float]]


@dataclass(frozen=True)
class Pose:
    pos: Vec
    yaw: float
    pitch: float
    fov: float


@dataclass(frozen=True)
class Segment:
    start: float  # clip seconds
    end: float
    kind: str  # "intro" or "downtime"
    style: str = ""


# ---- tracks ----------------------------------------------------------------


def _lerp(a: float, b: float, w: float) -> float:
    return a + (b - a) * w


def _lerp_angle(a: float, b: float, w: float) -> float:
    """Interpolate degrees along the shortest way round."""
    return a + ((b - a + 180.0) % 360.0 - 180.0) * w


def smoothstep(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def eye_pose_at(track: Track, t: float) -> Pose | None:
    """First-person pose (eye position, yaw, pitch) at clip time t, interpolated;
    None outside the track."""
    if not track or t < track[0][0] - 1e-9 or t > track[-1][0] + TRACK_SLACK:
        return None
    for (t0, p0, y0, a0), (t1, p1, y1, a1) in zip(track, track[1:]):
        if t0 <= t <= t1:
            w = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            pos = tuple(_lerp(u, v, w) for u, v in zip(p0, p1))
            return Pose((pos[0], pos[1], pos[2] + EYE_HEIGHT), _lerp_angle(y0, y1, w) % 360.0, _lerp(a0, a1, w), POV_FOV)
    _, p, yaw, pitch = track[-1]
    return Pose((p[0], p[1], p[2] + EYE_HEIGHT), yaw, pitch, POV_FOV)


# ---- style shots -----------------------------------------------------------


def look_at(source: Vec, target: Vec) -> tuple[float, float]:
    """(yaw, pitch) that point a camera at `source` toward `target`."""
    dx, dy, dz = (target[0] - source[0], target[1] - source[1], target[2] - source[2])
    return degrees(atan2(dy, dx)) % 360.0, -degrees(atan2(dz, hypot(dx, dy)))


def _around(head: Vec, yaw: float, angle: float, radius: float, height: float) -> Vec:
    """Point `radius` away from the head, `angle` degrees round from the player's
    facing direction (0 = in front, -90 = their right, 180 = behind)."""
    a = radians(yaw + angle)
    return head[0] + radius * cos(a), head[1] + radius * sin(a), head[2] + height


def style_pose(style: str, eye: Pose, u: float) -> Pose:
    """The style's shot for the player at `eye`; u in [0, 1] is how far through the
    shot we are (slow drift while it is held)."""
    head = (eye.pos[0], eye.pos[1], eye.pos[2] - 4.0)
    if style == "face":
        pos = _around(head, eye.yaw, _lerp(8.0, -14.0, u), _lerp(58.0, 50.0, u), -4.0)
        return Pose(pos, *look_at(pos, head), 85.0)
    if style == "profile":
        pos = _around(head, eye.yaw, _lerp(-95.0, -80.0, u), _lerp(70.0, 58.0, u), 2.0)
        return Pose(pos, *look_at(pos, head), 85.0)
    if style == "gun":
        pos = _around(head, eye.yaw, _lerp(15.0, 160.0, u), _lerp(115.0, 70.0, u), _lerp(-32.0, -2.0, u))
        chest = (head[0], head[1], head[2] - 18.0)
        return Pose(pos, *look_at(pos, chest), 80.0)
    if style == "tps":
        pos = _around(head, eye.yaw, _lerp(-150.0, -158.0, u), _lerp(78.0, 70.0, u), _lerp(16.0, 22.0, u))
        # over-the-shoulder shots look where the player looks
        return Pose(pos, eye.yaw, eye.pitch, 85.0)
    if style == "heli":
        height = _lerp(760.0, 620.0, u)
        pos = _around(head, eye.yaw, 180.0, height * 0.4, height)
        return Pose(pos, *look_at(pos, head), 90.0)
    raise ValueError(f"unknown cam style {style!r} (expected one of {', '.join(STYLES)})")


def blend(a: Pose, b: Pose, w: float) -> Pose:
    """a at w=0, b at w=1."""
    return Pose(
        tuple(_lerp(u, v, w) for u, v in zip(a.pos, b.pos)),  # type: ignore[arg-type]
        _lerp_angle(a.yaw, b.yaw, w) % 360.0,
        _lerp(a.pitch, b.pitch, w),
        _lerp(a.fov, b.fov, w),
    )


def segment_pose(segment: Segment, eye: Pose, t: float) -> Pose:
    """Camera pose at clip time t inside the segment. Intro: starts on the style shot,
    ends exactly on `eye`. Down time: starts and ends on `eye`, style shot between."""
    u = (t - segment.start) / (segment.end - segment.start)
    u = min(1.0, max(0.0, u))
    shot = style_pose(segment.style, eye, u)
    if segment.kind == "intro":
        return blend(shot, eye, smoothstep((u - OPENER_HOLD) / (1 - OPENER_HOLD)))
    out = smoothstep(u / DOWNTIME_OUT)
    back = smoothstep((u - (1 - DOWNTIME_BACK)) / DOWNTIME_BACK)
    return blend(eye, shot, out * (1 - back))


# ---- planning --------------------------------------------------------------


def _subtract(start: float, end: float, taken: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The parts of [start, end] not covered by the `taken` ranges."""
    free = [(start, end)]
    for a, b in sorted(taken):
        step: list[tuple[float, float]] = []
        for lo, hi in free:
            if b <= lo or a >= hi:
                step.append((lo, hi))
                continue
            if a > lo:
                step.append((lo, a))
            if b < hi:
                step.append((b, hi))
        free = step
    return free


def plan_segments(
    duration: float,
    kill_times: list[float],
    switch_times: list[float],
    intro: str | None,
    downtime: str | None,
    taken: list[tuple[float, float]] | None = None,
) -> list[Segment]:
    """Where generated cams replace the POV, in clip seconds.

    The opener runs from clip start until OPENER_SECONDS or one kill-padding before
    the first kill or camera switch, whichever is first (dropped when that leaves
    less than MIN_SEGMENT). Down-time cuts use the gaps between the POV windows
    around the kills (same padding rule as the splice planner), split at camera
    switches so the player never changes mid-cut, minus the ranges `taken` by a real
    cam path, capped at DOWNTIME_MAX each. `intro`/`downtime` are the style names.
    """
    taken = taken or []
    blockers = sorted([*kill_times, *switch_times])
    segments: list[Segment] = []
    opener_end = min(OPENER_SECONDS, duration)
    if blockers:
        opener_end = min(opener_end, blockers[0] - KILL_PAD)
    opener_end = round(opener_end, 2)
    if intro and opener_end >= MIN_SEGMENT:
        segments.append(Segment(0.0, opener_end, "intro", intro))
    if downtime:
        windows = _protected_windows(kill_times)
        gaps = []
        cursor = opener_end + POV_GAP if segments else 0.0
        for a, b in windows:
            gaps.append((cursor, min(a, duration)))
            cursor = max(cursor, b)
        gaps.append((cursor, duration))
        for lo, hi in gaps:
            cuts = [lo, *[s for s in sorted(switch_times) if lo < s < hi], hi]
            for a, b in zip(cuts, cuts[1:]):
                for fa, fb in _subtract(a, b, taken):
                    fb = min(fb, fa + DOWNTIME_MAX)
                    if fb - fa >= MIN_SEGMENT:
                        segments.append(Segment(round(fa, 2), round(fb, 2), "downtime", downtime))
    return sorted(segments, key=lambda s: s.start)


def pick_style(styles: list[str], seed: int) -> str:
    """One style from the list; deterministic per seed so a re-run picks the same."""
    return styles[0] if len(styles) == 1 else random.Random(seed).choice(styles)


def parse_styles(value: str) -> list[str]:
    """`face`, `face,tps` or `random` (every style) -> style names."""
    names = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not names:
        raise ValueError("no cam style given")
    if names == ["random"]:
        return list(STYLES)
    unknown = [n for n in names if n not in STYLES]
    if unknown:
        raise ValueError(f"unknown cam style {unknown[0]!r} (expected one of {', '.join(STYLES)} or random)")
    return list(dict.fromkeys(names))


# ---- path files ------------------------------------------------------------


def _quaternion(yaw: float, pitch: float) -> tuple[float, float, float, float]:
    """(w, x, y, z) of yaw about z then pitch (positive = down), matching the
    quaternion convention `campaths._forward` reads."""
    a, b = radians(yaw) / 2, radians(pitch) / 2
    return cos(a) * cos(b), -sin(a) * sin(b), cos(a) * sin(b), sin(a) * cos(b)


def build_keyframes(segments: list[Segment], eye_at) -> list[tuple[float, Pose]]:
    """Keyframes (clip seconds, pose) over every segment. `eye_at(t)` gives the
    first-person pose. The path is rebased to start at the first recorded frame, so
    when the first segment starts later the camera holds its first pose from t=0."""
    keys: list[tuple[float, Pose]] = []
    for segment in segments:
        steps = max(2, round((segment.end - segment.start) / KEY_STEP))
        for i in range(steps + 1):
            t = segment.start + (segment.end - segment.start) * i / steps
            eye = eye_at(t)
            if eye is None:
                raise ValueError(f"no player position at {t:.2f}s for the {segment.kind} cam")
            keys.append((round(t, 4), segment_pose(segment, eye, t)))
    if keys and keys[0][0] > 0:
        keys.insert(0, (0.0, keys[0][1]))
    return keys


def write_campath(file: Path, keys: list[tuple[float, Pose]]) -> Path:
    """Write HLAE campath XML (quaternion and Euler angles, no roll)."""
    rows = []
    previous: tuple[float, float, float, float] | None = None
    for t, pose in keys:
        quat = _quaternion(pose.yaw, pose.pitch)
        # q and -q are the same rotation; keep neighbours on the same side so
        # interpolation never takes the long way round.
        if previous and sum(a * b for a, b in zip(quat, previous)) < 0:
            quat = tuple(-c for c in quat)  # type: ignore[assignment]
        previous = quat
        qw, qx, qy, qz = quat
        x, y, z = pose.pos
        rows.append(
            f'<p t="{t:.4f}" x="{x:.3f}" y="{y:.3f}" z="{z:.3f}" fov="{pose.fov:.2f}" '
            f'rx="0" ry="{pose.pitch:.4f}" rz="{pose.yaw:.4f}" '
            f'qw="{qw:.6f}" qx="{qx:.6f}" qy="{qy:.6f}" qz="{qz:.6f}"/>'
        )
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("<campath><points>" + "".join(rows) + "</points></campath>", encoding="utf-8")
    return file
