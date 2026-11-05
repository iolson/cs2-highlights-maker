"""`highlights broll`: cam-path-only clips for pre-edit build-up, no highlight needed.

Each clip is one of your cam paths recorded over a stretch of live play. `empty` mode only uses
stretches where no living player is in view (establishing shots); `players` mode only uses
stretches where players are in view for most of the clip (the "random people" mode). Windows,
rounds and paths are picked at random (`--seed` repeats a pick). Planned from data only, so
`--json-only` previews everything; recording is one game session per demo.
"""

from __future__ import annotations

import json
import random
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from highlights.config import Config
from highlights.demo.parser import DemoData
from highlights.models import CameraAction, Sequence
from highlights.recording.campaths import CamPath, pose_at
from highlights.recording.splice_plan import VIEW_MARGIN, _in_view
from highlights.util.log import info, warn

MODES = ("empty", "players")
SAMPLE_STEP = 0.5  # seconds between visibility checks (and between queried demo ticks)
START_STEP = 2.0  # candidate window starts are multiples of this inside a round's live phase
ROUND_LEAD = 2.0  # seconds after freeze time before a window may start
MIN_LENGTH = 3.0  # shorter clips are not worth a build-up shot
MIN_PRESENCE = 0.6  # players mode: share of samples with at least one player in view
EMPTY_MARGIN = 1.15  # empty mode treats a player just outside the frame as in view
ATTEMPTS = 4000  # random (round, start, path) tries before giving up
Point = tuple[float, float, float]
# tick -> [(steamid, xyz, alive)] of every player the demo has at that tick
Sampler = Callable[[DemoData, list[int]], dict[int, list[tuple[str, Point, bool]]]]


@dataclass
class BrollShot:
    round_index: int
    path: CamPath
    start_tick: int
    end_tick: int
    seconds: float
    presence: float  # share of samples with a player in view
    visible: list[str] = field(default_factory=list)  # steamids seen at some point
    anchor: str = ""  # a living player at the start, only to give the recording a camera


def sample_players(demo: DemoData, ticks: list[int]) -> dict[int, list[tuple[str, Point, bool]]]:
    """Every player's position at the given ticks (an unreadable demo gives an empty result)."""
    from highlights.demo.parser import is_parser_error
    from highlights.spatial import _query_positions

    try:
        positions, _, _ = _query_positions(demo.path, ticks)
    except BaseException as exc:
        if not is_parser_error(exc):
            raise
        return {}
    return {
        tick: [
            (sid, (v["xyz"]["x"], v["xyz"]["y"], v["xyz"]["z"]), v.get("is_alive") is not False)
            for sid, v in players.items()
        ]
        for tick, players in positions.items()
    }


def _evaluate(
    path: CamPath,
    seconds: float,
    start_tick: int,
    tickrate: float,
    snapshots: dict[int, list[tuple[str, Point, bool]]],
    margin: float,
) -> tuple[float, list[str], str] | None:
    """(presence, steamids seen, anchor) for a window, None when position data is missing."""
    steps = int(seconds / SAMPLE_STEP) + 1
    seen: list[str] = []
    hits = 0
    anchor = ""
    for i in range(steps):
        t = min(i * SAMPLE_STEP, seconds)
        pose = pose_at(path, t)
        players = snapshots.get(start_tick + round(t * tickrate))
        if pose is None or players is None:
            return None
        here = False
        for sid, xyz, alive in players:
            if not alive:
                continue
            if not anchor:
                anchor = sid
            if _in_view(pose[0], pose[1], pose[2], xyz, margin):
                here = True
                if sid not in seen:
                    seen.append(sid)
        hits += here
    return hits / steps, seen, anchor


def plan_demo_broll(
    demo: DemoData,
    paths: list[CamPath],
    mode: str,
    count: int,
    max_length: float,
    seed: int | None = None,
    sampler: Sampler = sample_players,
) -> list[BrollShot]:
    """Up to `count` random cam-path windows that satisfy the mode, none overlapping."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    rng = random.Random(seed)
    tickrate = demo.tickrate
    usable = [p for p in paths if min(p.duration, max_length) >= MIN_LENGTH]
    rounds = [r for r in demo.rounds if (r.end_tick - r.freeze_end_tick) / tickrate >= ROUND_LEAD + MIN_LENGTH]
    if not usable or not rounds:
        return []
    rng.shuffle(rounds)
    rounds = rounds[: max(3, count * 2)]

    # One query serves every candidate: the round's live phase on the sampling grid.
    step_ticks = max(1, round(SAMPLE_STEP * tickrate))
    live = {r.index: range(r.freeze_end_tick + int(ROUND_LEAD * tickrate), r.end_tick, step_ticks) for r in rounds}
    snapshots = sampler(demo, sorted({tick for ticks in live.values() for tick in ticks}))
    if not snapshots:
        return []

    start_step_ticks = max(step_ticks, round(START_STEP * tickrate) // step_ticks * step_ticks)
    chosen: list[BrollShot] = []
    used_paths: set[str] = set()
    margin = EMPTY_MARGIN if mode == "empty" else VIEW_MARGIN
    for attempt in range(ATTEMPTS):
        if len(chosen) >= count:
            break
        round_ = rng.choice(rounds)
        path = rng.choice(usable)
        if path.name in used_paths and attempt < ATTEMPTS * 0.6 and len(usable) > len(used_paths):
            continue  # prefer a variety of paths
        seconds = min(path.duration, max_length)
        first = live[round_.index].start
        last = round_.end_tick - int(seconds * tickrate)
        if last < first:
            continue
        start_tick = first + rng.randrange(0, (last - first) // start_step_ticks + 1) * start_step_ticks
        end_tick = start_tick + int(seconds * tickrate)
        if any(s.round_index == round_.index and start_tick < s.end_tick and s.start_tick < end_tick for s in chosen):
            continue
        result = _evaluate(path, seconds, start_tick, tickrate, snapshots, margin)
        if result is None:
            continue
        presence, visible, anchor = result
        if mode == "empty" and presence > 0:
            continue
        if mode == "players" and presence < MIN_PRESENCE:
            continue
        chosen.append(BrollShot(round_.index, path, start_tick, end_tick, seconds, presence, visible, anchor))
        used_paths.add(path.name)
    return sorted(chosen, key=lambda s: s.start_tick)


def shot_sequences(demo: DemoData, shots: list[BrollShot], mode: str) -> list[Sequence]:
    """One cam-path sequence per shot (the camera player only seeds the recording setup)."""
    sequences = []
    for number, shot in enumerate(shots, 1):
        anchor = shot.anchor if shot.anchor in demo.slots else next(iter(demo.slots), "")
        if not anchor:
            raise ValueError("The demo has no player slots to aim a camera with")
        sequences.append(
            Sequence(
                number=number,
                start_tick=shot.start_tick,
                end_tick=shot.end_tick,
                cameras=[CameraAction(shot.start_tick, anchor)],
                label=f"B-roll ({mode}) — {shot.path.name} — R{shot.round_index + 1}",
                score=0.0,
                round_index=shot.round_index,
                campath=shot.path.file,
            )
        )
    return sequences


def shot_report(demo: DemoData, label: str, mode: str, shot: BrollShot, filename: str) -> dict:
    return {
        "kind": "broll",
        "demo": label,
        "map": demo.map_name,
        "mode": mode,
        "file": filename,
        "round": shot.round_index + 1,
        "campath": shot.path.name,
        "start_tick": shot.start_tick,
        "end_tick": shot.end_tick,
        "duration_seconds": round(shot.seconds, 2),
        "players_in_view_share": round(shot.presence, 2),
        "players_in_view": [{"steamid": s, "name": demo.player_names.get(s, "")} for s in shot.visible],
    }


def broll_names(label: str, mode: str, count: int) -> list[str]:
    return [f"{label}-broll-{mode}-{n}" for n in range(1, count + 1)]


def run_broll(
    jobs: list,
    config: Config,
    campaths_for: Callable[[str], list[CamPath]],
    mode: str,
    count: int,
    max_length: float,
    output_dir: Path,
    seed: int | None = None,
    json_only: bool = False,
    keep_clips: bool = False,
    sampler: Sampler | None = None,
) -> list[Path]:
    """Plan and (unless json_only) record B-roll for every demo job; returns the written clips."""
    written: list[Path] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for index, job in enumerate(jobs):
        paths = campaths_for(job.demo.map_name)
        shots = plan_demo_broll(job.demo, paths, mode, count, max_length, None if seed is None else seed + index, sampler or sample_players)
        if not shots:
            warn(f"{job.label}: no {mode} window found with these cam paths — try --max-length or another seed")
            continue
        names = broll_names(job.label, mode, len(shots))
        for shot, name in zip(shots, names):
            who = ", ".join(job.demo.player_names.get(s, s) for s in shot.visible) or "nobody"
            info(f"{name}: R{shot.round_index + 1} {shot.path.name} {shot.seconds:.1f}s — in view: {who}")
            (output_dir / f"{name}.json").write_text(
                json.dumps(shot_report(job.demo, job.label, mode, shot, f"{name}.mp4"), indent=2), encoding="utf-8"
            )
        if json_only:
            continue
        written += _record_job(job, shot_sequences(job.demo, shots, mode), names, config, output_dir, keep_clips)
    return written


def _record_job(job, sequences: list[Sequence], names: list[str], config: Config, output_dir: Path, keep_clips: bool) -> list[Path]:
    from highlights.recording.recorder import RecordingSetupError, record, resolve_tools
    from highlights.video.postprocess import process_clip

    try:
        outputs = record(job.demo, sequences, config, job.work_dir)
    except RecordingSetupError as exc:
        warn(f"{job.label}: recording failed, skipping this demo: {exc}")
        return []
    tools = resolve_tools(config, auto_install=False)
    written = []
    for output in outputs:
        clip = process_clip(tools.ffmpeg_exe, output, job.work_dir / "clips", False)
        target = output_dir / f"{names[output.sequence.number - 1]}.mp4"
        shutil.copy2(clip, target)
        written.append(target)
        info(f"Wrote {target}")
    if len(written) < len(sequences):
        warn(f"{job.label}: {len(sequences) - len(written)} B-roll clip(s) failed to record")
    if not keep_clips:
        shutil.rmtree(job.work_dir, ignore_errors=True)
    return written
