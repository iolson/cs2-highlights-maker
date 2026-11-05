"""`highlights batch`: one finished clip + JSON report per highlight, across several demos.

The best N moments of the (optionally player-filtered) pool of all demos each become their own
file, `<label>-<player>-<n>.mp4` + `.json`, shaped like a single-clip `run` so a separate
compilation script can join them. Selection is global; recording is one game session per demo.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from highlights.analysis.selection import build_sequences, eligible_moments, pick_rows, round_allowed
from highlights.config import Config
from highlights.demo.parser import DemoData
from highlights.demo.players import PlayerNotFound, resolve_player
from highlights.models import Moment, Sequence
from highlights.util.log import info, warn


def parse_demo_arg(value: str) -> tuple[str, Path]:
    """`LABEL=PATH` or just `PATH` (the label is then the file name without extension)."""
    label, sep, raw = value.partition("=")
    if sep and label.strip() and raw.strip():
        return label.strip(), Path(raw.strip())
    return Path(value).stem, Path(value)


def slugify(name: str, fallback: str = "player") -> str:
    """Lowercase letters, digits and underscores only ("deviN-" -> "devin")."""
    return re.sub(r"[^a-z0-9_]", "", name.lower()) or fallback


def team_steamids(names_file: Path, teams: list[str]) -> set[str]:
    """SteamID64s whose `team` in a cotw-style names.json is one of `teams` (case-insensitive)."""
    wanted = {team.lower() for team in teams}
    data = json.loads(names_file.read_text(encoding="utf-8"))
    return {
        key
        for key, value in data.items()
        if key.isdigit() and isinstance(value, dict) and str(value.get("team", "")).lower() in wanted
    }


def present_players(demo: DemoData, queries: list[str], team_ids: set[str]) -> tuple[set[str], list[str]]:
    """The filter's players that played in this demo, and the queries that matched nobody.

    Unlike `run`/`top`, absence is normal here (a team plays in only some of the demos)."""
    found = {sid for sid in team_ids if sid in demo.player_names}
    missing: list[str] = []
    for query in (q.strip() for item in queries for q in item.split(",")):
        if not query:
            continue
        try:
            found.add(resolve_player(demo, query))
        except PlayerNotFound as exc:
            if "several" in str(exc):
                warn(f"{demo.path.name}: {exc} — skipping that name")
            missing.append(query)
    return found, missing


@dataclass
class DemoJob:
    label: str
    path: Path
    demo: DemoData
    work_dir: Path


def clip_names(rows: list[tuple[str, Moment]]) -> list[str]:
    """`<label>-<player>-<n>` for each (label, moment) in rank order; n counts per label and player."""
    counts: dict[tuple[str, str], int] = {}
    names = []
    for label, moment in rows:
        key = (label, slugify(moment.name, moment.steamid))
        counts[key] = counts.get(key, 0) + 1
        names.append(f"{key[0]}-{key[1]}-{counts[key]}")
    return names


def clip_group(sequences: list[Sequence], spatial: dict[int, dict], pov: Sequence) -> tuple[list[Sequence], dict[int, dict]]:
    """A POV sequence and its cinematic takes, renumbered 1..k, with their spatial data."""
    group = [pov, *(s for s in sequences if s.cinematic_of == pov.number)]
    numbering = {s.number: i for i, s in enumerate(group, 1)}
    renumbered = [
        replace(s, number=numbering[s.number], cinematic_of=numbering.get(s.cinematic_of) if s.cinematic_of else None)
        for s in group
    ]
    return renumbered, {numbering[s.number]: spatial[s.number] for s in group if s.number in spatial}


@dataclass
class ClipPlan:
    name: str
    pov: Sequence  # the POV sequence as numbered in the demo's recording
    group: list[Sequence]  # POV + cinematic takes, numbered 1..k (what the clip's JSON describes)
    spatial: dict[int, dict]


def plan_demo(
    job: DemoJob,
    picked: list[tuple[Moment, str]],
    config: Config,
    cinematic: bool,
    cinematic_path: str | None,
    hold: bool,
    align: bool,
    intro_cam: list[str] | None,
    downtime_cam: list[str] | None,
    tilt_mode: str = "off",
) -> tuple[list[Sequence], list[ClipPlan]]:
    """Every sequence to record for this demo (POV plus cinematic/generated takes) and the clips they make."""
    from highlights import cli
    from highlights.spatial import build_spatial_metadata, sample_pov_tracks

    demo = job.demo
    sequences = build_sequences([m for m, _ in picked], demo, config.selection, config.recording, merge=False)
    spatial = build_spatial_metadata(demo, sequences)
    names = {id(m): name for m, name in picked}

    if cinematic:
        from highlights.recording.cinematic import add_cinematic_sequences

        try:
            campaths, place_map, labels = cli._load_cinematic_inputs(config, demo.map_name)
            forced = None
            if cinematic_path:
                forced = next((p for p in campaths if p.name.lower() == cinematic_path.lower()), None)
                if forced is None:
                    warn(f"{job.label}: no cam path named {cinematic_path!r} for {demo.map_name}, no cinematic")
            if forced is not None or not cinematic_path:
                sequences, spatial = add_cinematic_sequences(
                    sequences,
                    campaths,
                    spatial,
                    demo.tickrate,
                    splice=True,
                    forced=forced,
                    place_map=place_map,
                    labels=labels,
                    hold=hold or align,
                    align=align,
                )
        except Exception as exc:  # a map without cam paths must not stop the batch
            warn(f"{job.label}: no cinematic ({exc})")

    if intro_cam or downtime_cam:
        from highlights.recording.dyn_sequences import add_dynamic_sequences

        sequences, spatial = add_dynamic_sequences(
            sequences, spatial, demo, sample_pov_tracks, job.work_dir / "dyncams", intro_cam, downtime_cam
        )

    if tilt_mode != "off":
        from highlights.analysis.tilt import plan_sequences as plan_tilt

        sequences = plan_tilt(sequences, spatial, demo.tickrate, tilt_mode)

    clips = []
    for pov in (s for s in sequences if s.cinematic_of is None):
        group, group_spatial = clip_group(sequences, spatial, pov)
        clips.append(ClipPlan(names[id(pov.moments[0])], pov, group, group_spatial))
    return sequences, clips


def run_batch(
    jobs: list[DemoJob],
    config: Config,
    queries: list[str],
    team_ids: set[str],
    output_dir: Path,
    json_only: bool = False,
    keep_clips: bool = False,
    timing_offset: float = 0.0,
    cinematic: bool = False,
    cinematic_path: str | None = None,
    hold: bool = False,
    align: bool = False,
    intro_cam: list[str] | None = None,
    downtime_cam: list[str] | None = None,
    tilt_mode: str = "off",
    round_rules: list | None = None,
) -> list[Path]:
    """Select the best moments across all demos, then record each demo and write one clip + JSON per moment."""
    import click

    from highlights import cli
    from highlights.report import print_top_table, write_report

    filtered = bool(queries or team_ids)
    rows: list[tuple[DemoData, Moment]] = []
    label_of: dict[int, str] = {}
    for job in jobs:
        label_of[id(job.demo)] = job.label
        moments = cli._scored_moments(job.demo, config)
        steamids = None
        if filtered:
            steamids, missing = present_players(job.demo, queries, team_ids)
            if not steamids:
                warn(f"{job.label}: none of the players are in this demo, skipping it")
                continue
            if missing:
                info(f"{job.label}: not in this demo: {', '.join(missing)}")
        rows.extend(
            (job.demo, m)
            for m in eligible_moments(moments, config.selection, steamids)
            if round_allowed(round_rules or [], job.label, m.round_index)
        )
    if not rows:
        raise click.ClickException("No highlights matched the criteria — try --min-kills 1 or check the demos")

    picked, total = pick_rows(rows, config.selection, config.recording)
    if not picked:
        raise click.ClickException("No clip fits in --max-duration — raise it")
    names = clip_names([(label_of[id(demo)], moment) for demo, moment in picked])
    print_top_table(picked, clips=names, labels=[label_of[id(demo)] for demo, _ in picked])
    info(f"{len(picked)} clips, about {total:.0f}s in total, into {output_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for job in jobs:
        mine = [(moment, name) for (demo, moment), name in zip(picked, names) if demo is job.demo]
        if not mine:
            continue
        info(f"[bold]{job.label}[/bold]: {len(mine)} clip(s)")
        sequences, clips = plan_demo(
            job, mine, config, cinematic, cinematic_path, hold, align, intro_cam, downtime_cam, tilt_mode
        )
        for clip in clips:
            write_report(output_dir / f"{clip.name}.json", job.demo, clip.group, spatial_by_sequence=clip.spatial)
        if json_only:
            continue
        written += _record_demo(
            job, sequences, clips, config, output_dir, keep_clips, timing_offset
        )
    return written


def _record_demo(
    job: DemoJob,
    sequences: list[Sequence],
    clips: list[ClipPlan],
    config: Config,
    output_dir: Path,
    keep_clips: bool,
    timing_offset: float,
) -> list[Path]:
    from highlights import cli
    from highlights.recording.recorder import RecordingSetupError, record, resolve_tools
    from highlights.report import finalize_report
    from highlights.video.ffmpeg import probe_duration
    from highlights.video.postprocess import process_clip

    try:
        outputs = record(job.demo, sequences, config, job.work_dir)
    except RecordingSetupError as exc:
        warn(f"{job.label}: recording failed, skipping this demo: {exc}")
        return []
    if not outputs:
        warn(f"{job.label}: recording produced no clips, skipping this demo")
        return []

    tools = resolve_tools(config, auto_install=False)
    clips_dir = job.work_dir / "clips"
    info(f"{job.label}: recording done, post-processing {len(outputs)} sequence(s)...")
    recorded = []
    for index, o in enumerate(outputs, 1):
        info(f"{job.label}: preparing clip {index}/{len(outputs)}...")
        recorded.append((o, process_clip(tools.ffmpeg_exe, o, clips_dir, False)))

    if any(o.sequence.splice_segments for o, _ in recorded):
        recorded = cli._splice_cinematics(
            tools.ffmpeg_exe, recorded, clips_dir, config.recording.crf, (config.recording.width, config.recording.height)
        )

    name_of = {id(c.pov.moments[0]): c.name for c in clips}
    written = []
    for output, clip in recorded:
        sequence = output.sequence
        if sequence.cinematic_of is not None:
            continue
        name = name_of[id(sequence.moments[0])]
        target = output_dir / f"{name}.mp4"
        shutil.copy2(clip, target)
        report_path = output_dir / f"{name}.json"
        finalize_report(
            report_path,
            {1: probe_duration(tools.ffmpeg_exe, target)},
            offset_seconds=timing_offset,
        )
        written.append(target)
        info(f"Wrote {target}")
    missing = [c.name for c in clips if output_dir.joinpath(f"{c.name}.mp4") not in written]
    if missing:
        warn(f"{job.label}: no recording for {', '.join(missing)}")
    if not keep_clips:
        shutil.rmtree(job.work_dir, ignore_errors=True)
    return written

