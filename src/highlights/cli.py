"""CLI entry point.

`highlights match.dem -o out.mp4` is the main flow (the `run` subcommand is
implied). `highlights doctor` validates the environment.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import click

from highlights import __version__
from highlights.analysis.clutch import attach_clutches, detect_clutches
from highlights.analysis.moments import cluster_moments
from highlights.analysis.scoring import score_moments
from highlights.analysis.selection import clip_seconds, eligible_moments, pick_rows, select_sequences
from highlights.config import Config, load_config
from highlights.demo.parser import DemoData, DemoParseError, ensure_dem_file, parse_demo
from highlights.demo.players import PlayerNotFound, resolve_players
from highlights.models import CameraAction, Moment, Sequence
from highlights.places import PlaceMap, load_places, places_dir, save_places
from highlights.report import (
    finalize_report,
    print_sequences_table,
    print_suggestions_table,
    print_top_table,
    write_report,
)
from highlights.spatial import sample_places
from highlights.util.log import console, error, info, warn


class DefaultRunGroup(click.Group):
    """Treat `highlights match.dem` as `highlights run match.dem`."""

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            command = self.get_command(ctx, "run")
            return "run", command, args


@click.group(cls=DefaultRunGroup)
@click.version_option(__version__)
def main() -> None:
    """Generate a highlights MP4 from a CS2 demo."""


def _resolve_work_dir(config: Config, demo_path: Path) -> Path:
    if config.paths.work_dir != "auto":
        return Path(config.paths.work_dir) / demo_path.stem
    # Not %TEMP%: Store Python virtualizes Temp/AppData writes (see config.py).
    from highlights.config import app_data_dir

    return app_data_dir() / "work" / demo_path.stem


def _scored_moments(demo: DemoData, config: Config) -> list[Moment]:
    moments = cluster_moments(demo.kills, demo.tickrate, config.selection.kill_gap_seconds)
    clutches = detect_clutches(demo)
    moments = attach_clutches(moments, clutches, demo)
    score_moments(moments, demo)
    return moments


def _analyze(demo: DemoData, config: Config, queries: list[str]) -> list[Sequence]:
    moments = _scored_moments(demo, config)

    player_steamids: list[str] | None = None
    if queries:
        player_steamids = resolve_players(demo, queries)
        names = ", ".join(f"{demo.player_names[sid]} ({sid})" for sid in player_steamids)
        info(f"Filtering highlights to players: {names}")

    return select_sequences(moments, demo, config.selection, config.recording, player_steamids)


def _load_cinematic_inputs(config: Config, map_name: str) -> tuple[list, PlaceMap | None, dict[str, str]]:
    """Cam paths, place map and labels for a cinematic run (shared by --cinematic and --suggest-cinematic)."""
    from highlights.places import load_places
    from highlights.recording.campaths import load_campaths, resolve_campaths_dir
    from highlights.recording.path_routes import load_labels

    campaths_dir = resolve_campaths_dir(config)
    campaths = load_campaths(campaths_dir, map_name)
    if not campaths:
        raise click.ClickException(
            f"No cam paths for {map_name} in {campaths_dir} — "
            "put them in a folder named after the map (see README, 'Cinematic cam paths')"
        )
    info(f"Cinematic: {len(campaths)} cam path(s) found for {map_name}")
    place_map = load_places(map_name)
    if place_map is None:
        warn(
            f"No place map for {map_name} — run `highlights places build DEMO...` "
            "to see where cam paths go"
        )
    labels = load_labels(campaths_dir, map_name)
    return campaths, place_map, labels


def _parse_cam_styles(value: str | None) -> list[str] | None:
    """Click callback for --intro-cam/--downtime-cam: style names, or None when unset."""
    if value is None:
        return None
    from highlights.recording.dyncams import parse_styles

    try:
        return parse_styles(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc))


def _parse_player_names(values: tuple[str, ...]) -> dict[str, str]:
    """Parse `STEAMID=NAME` strings into a steamid -> name mapping (later values win)."""
    result: dict[str, str] = {}
    for value in values:
        steamid, sep, name = value.partition("=")
        steamid, name = steamid.strip(), name.strip()
        if not sep or not steamid.isdigit() or not name:
            raise click.BadParameter(f"expected STEAMID=NAME, got {value!r}")
        result[steamid] = name
    return result


@main.command()
@click.argument("demo", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None, help="Output MP4 path")
@click.option("--player", default=None, help="Only this player's highlights (steamid64 or name)")
@click.option(
    "--players",
    "players",
    multiple=True,
    help="Only these players' highlights: comma-separated and/or repeated (steamid64 or name)",
)
@click.option("--top", type=int, default=None, help="Max number of highlights")
@click.option("--max-duration", type=float, default=None, help="Max total video length, seconds")
@click.option("--min-kills", type=int, default=None, help="Min kills for a moment to qualify")
@click.option("--fps", type=int, default=None, help="Recording framerate")
@click.option("--width", type=int, default=None, help="Recording width")
@click.option("--height", type=int, default=None, help="Recording height")
@click.option("--crf", type=int, default=None, help="x264 CRF for recorded clips")
@click.option(
    "--player-name",
    "player_names",
    multiple=True,
    metavar="STEAMID=NAME",
    help="Rename a player in the recording (steamid64=Display Name); repeatable, overrides config.toml",
)
@click.option(
    "--cmd",
    "extra_commands",
    multiple=True,
    metavar="COMMAND",
    help="Extra console command run at every clip's start (repeatable, added after config.toml's extra_commands)",
)
@click.option("--json-only", is_flag=True, help="Analyze only: write highlights.json and stop")
@click.option("--keep-clips", is_flag=True, help="Keep the work dir with individual clips")
@click.option("--overlays/--no-overlays", default=False, help="Draw a text label on each clip")
@click.option(
    "--transitions",
    type=click.Choice(["none", "fade"]),
    default="none",
    help="Transition between clips (fade re-encodes, slower)",
)
@click.option(
    "--suggest-cinematic",
    "suggest_cinematic",
    is_flag=True,
    help="Show which clips suit a cinematic and the best cam path for each "
    "(needs cam paths for the map; nothing is recorded for this)",
)
@click.option(
    "--cinematic",
    is_flag=True,
    help="Also record each play through your own cam paths (see README, campaths/)",
)
@click.option(
    "--cinematic-takes",
    type=click.IntRange(min=1),
    default=1,
    help="Cam paths per play with --cinematic (best matches first)",
)
@click.option(
    "--cinematic-path",
    default=None,
    help="Use this cam path (file name without extension, e.g. B5) for every play instead of matching",
)
@click.option(
    "--cinematic-mode",
    type=click.Choice(["append", "splice"]),
    default="append",
    help="append: cinematic takes follow the POV clip; splice: cut the cinematic into the "
    "POV clip during down time between kills",
)
@click.option(
    "--slow-mode",
    type=click.Choice(["off", "auto"]),
    default="off",
    help="auto: give the clips whose kill is most worth it (4K/ace finish, clutch finish, knife, noscope, "
    "wallbang...) a short slow-motion beat; most clips get none",
)
@click.option(
    "--tilt-mode",
    type=click.Choice(["off", "auto", "on"]),
    default="off",
    help="Tilt-shift look (sharp middle band, blurred top/bottom) on cam-path shots; needs --cinematic "
    "--cinematic-mode splice. auto: only wide, elevated shots; on: every cam-path shot",
)
@click.option(
    "--campath-hold",
    is_flag=True,
    help="With --cinematic: keep each cam path's last pose until the clip ends instead of returning to the POV",
)
@click.option(
    "--campath-align",
    is_flag=True,
    help="With --cinematic --cinematic-mode splice: also start each cam path late, at the offset that "
    "sees the player longest (implies --campath-hold)",
)
@click.option(
    "--intro-cam",
    default=None,
    callback=lambda ctx, param, value: _parse_cam_styles(value),
    help="Open every clip with a generated cam that eases into the POV: face, profile, gun, tps, "
    "heli, a comma-separated list, or random",
)
@click.option(
    "--downtime-cam",
    default=None,
    callback=lambda ctx, param, value: _parse_cam_styles(value),
    help="Cut a generated cam into the down time between kills wherever no cam path covers it "
    "(same styles as --intro-cam)",
)
@click.option(
    "--vertical",
    type=click.Choice(["blur", "crop"]),
    default=None,
    help="Also write a 1080x1920 version (<output>-vertical.mp4): blur = footage over a blurred "
    "copy of itself, crop = centre slice",
)
@click.option(
    "--vertical-killfeed",
    is_flag=True,
    help="With --vertical blur: show the kill feed, enlarged, below the footage",
)
@click.option(
    "--timing-offset",
    type=float,
    default=0.0,
    help="Seconds added to kill times in the report (calibrated capture-start offset)",
)
def run(
    demo: Path,
    output: Path | None,
    player: str | None,
    players: tuple[str, ...],
    top: int | None,
    max_duration: float | None,
    min_kills: int | None,
    fps: int | None,
    width: int | None,
    height: int | None,
    crf: int | None,
    player_names: tuple[str, ...],
    extra_commands: tuple[str, ...],
    json_only: bool,
    keep_clips: bool,
    overlays: bool,
    transitions: str,
    suggest_cinematic: bool,
    cinematic: bool,
    cinematic_takes: int,
    cinematic_path: str | None,
    cinematic_mode: str,
    slow_mode: str,
    tilt_mode: str,
    campath_hold: bool,
    campath_align: bool,
    intro_cam: list[str] | None,
    downtime_cam: list[str] | None,
    vertical: str | None,
    vertical_killfeed: bool,
    timing_offset: float,
) -> None:
    """Parse DEMO, detect highlights, record them in-game and produce an MP4."""
    if (campath_hold or campath_align) and not cinematic:
        raise click.UsageError("--campath-hold/--campath-align need --cinematic")
    if campath_align and cinematic_mode != "splice":
        raise click.UsageError("--campath-align needs --cinematic-mode splice")
    if tilt_mode != "off" and not (cinematic and cinematic_mode == "splice"):
        raise click.UsageError("--tilt-mode needs --cinematic --cinematic-mode splice")
    if vertical_killfeed and vertical != "blur":
        raise click.UsageError("--vertical-killfeed needs --vertical blur")
    if suggest_cinematic and cinematic:
        raise click.UsageError("--suggest-cinematic is for previewing; use it without --cinematic")
    if suggest_cinematic and (intro_cam or downtime_cam):
        raise click.UsageError("--suggest-cinematic cannot be combined with --intro-cam/--downtime-cam")
    config = load_config()
    for section, name, value in (
        (config.selection, "top", top),
        (config.selection, "max_duration", max_duration),
        (config.selection, "min_kills", min_kills),
        (config.recording, "fps", fps),
        (config.recording, "width", width),
        (config.recording, "height", height),
        (config.recording, "crf", crf),
    ):
        if value is not None:
            setattr(section, name, value)

    config.recording.player_names.update(_parse_player_names(player_names))
    config.recording.extra_commands = [*config.recording.extra_commands, *extra_commands]

    output = output or demo.with_name(demo.stem + "-highlights.mp4")
    work_dir = _resolve_work_dir(config, demo)
    work_dir.mkdir(parents=True, exist_ok=True)

    with console.status("Parsing demo..."):
        demo_path = ensure_dem_file(demo, work_dir)
        try:
            parsed = parse_demo(demo_path)
        except DemoParseError as exc:
            raise click.ClickException(str(exc))
    info(
        f"Parsed [bold]{parsed.map_name}[/bold]: {len(parsed.rounds)} rounds, "
        f"{len(parsed.kills)} kills, tickrate {parsed.tickrate:.0f}"
    )

    try:
        queries = [*([player] if player else []), *players]
        sequences = _analyze(parsed, config, queries)
    except PlayerNotFound as exc:
        raise click.ClickException(str(exc))

    if not sequences:
        raise click.ClickException(
            "No highlights matched the criteria — try --min-kills 1 or check the demo"
        )

    from highlights.spatial import build_spatial_metadata

    suggestions = None
    spatial = None
    if not cinematic:
        spatial = build_spatial_metadata(parsed, sequences)
    if suggest_cinematic:
        from highlights.recording.suggest import suggest_cinematics

        campaths, place_map, labels = _load_cinematic_inputs(config, parsed.map_name)
        suggestions = suggest_cinematics(
            sequences, campaths, spatial, parsed.tickrate, place_map=place_map, labels=labels
        )
    if cinematic:
        from highlights.recording.cinematic import add_cinematic_sequences

        campaths, place_map, labels = _load_cinematic_inputs(config, parsed.map_name)
        forced = None
        if cinematic_path:
            forced = next((p for p in campaths if p.name.lower() == cinematic_path.lower()), None)
            if forced is None:
                raise click.ClickException(
                    f"No cam path named {cinematic_path!r} for {parsed.map_name}; "
                    f"available: {', '.join(p.name for p in campaths)}"
                )
        sequences, spatial = add_cinematic_sequences(
            sequences,
            campaths,
            build_spatial_metadata(parsed, sequences),
            parsed.tickrate,
            cinematic_takes,
            splice=cinematic_mode == "splice",
            forced=forced,
            place_map=place_map,
            labels=labels,
            hold=campath_hold or campath_align,
            align=campath_align,
        )

    if intro_cam or downtime_cam:
        from highlights.recording.dyn_sequences import add_dynamic_sequences
        from highlights.spatial import sample_pov_tracks

        sequences, spatial = add_dynamic_sequences(
            sequences,
            spatial,
            parsed,
            sample_pov_tracks,
            work_dir / "dyncams",
            intro_cam,
            downtime_cam,
        )

    if tilt_mode != "off":
        from highlights.analysis.tilt import plan_sequences as plan_tilt

        sequences = plan_tilt(sequences, spatial, parsed.tickrate, tilt_mode)
        info(f"Tilt-shift: {sum(1 for s in sequences if s.tilt)} cam-path shot(s)")

    if slow_mode == "auto":
        from highlights.analysis.slowmo import plan_sequences

        sequences = plan_sequences(sequences, parsed.tickrate, config.selection.max_duration)
        planned = sum(1 for s in sequences if s.slowmo)
        info(f"Slow-motion: {planned} of {sum(1 for s in sequences if s.cinematic_of is None)} clips")

    print_sequences_table(parsed, sequences, spatial)
    if suggestions is not None:
        print_suggestions_table(parsed, sequences, suggestions)
    report_path = output.with_suffix(".json")
    write_report(report_path, parsed, sequences, spatial_by_sequence=spatial, suggestions=suggestions)
    info(f"Report written to {report_path}")

    if json_only:
        return

    total = sum(s.duration_seconds(parsed.tickrate) for s in sequences)
    info(f"Estimated recording time: at least {int(total + len(sequences) * 20 + 90)}s (runs the game)")

    from highlights.recording.recorder import RecordingSetupError, record, resolve_tools
    from highlights.video.concat import concat_clips, concat_clips_with_fade
    from highlights.video.postprocess import process_clip

    started = time.monotonic()
    try:
        outputs = record(parsed, sequences, config, work_dir)
    except RecordingSetupError as exc:
        raise click.ClickException(str(exc))
    if not outputs:
        raise click.ClickException("Recording produced no clips — see warnings above")

    tools = resolve_tools(config, auto_install=False)
    with console.status("Post-processing clips..."):
        recorded = [(o, process_clip(tools.ffmpeg_exe, o, work_dir / "clips", overlays)) for o in outputs]
        if any(o.sequence.splice_segments for o, _ in recorded):
            recorded = _splice_cinematics(
                tools.ffmpeg_exe,
                recorded,
                work_dir / "clips",
                config.recording.crf,
                (config.recording.width, config.recording.height),
            )
        if any(o.sequence.slowmo for o, _ in recorded):
            recorded = _apply_slowmo(tools.ffmpeg_exe, recorded, work_dir / "clips", config.recording.crf)
        outputs = [o for o, _ in recorded]
        clips = [c for _, c in recorded]
        if transitions == "fade":
            concat_clips_with_fade(tools.ffmpeg_exe, clips, output)
        else:
            concat_clips(tools.ffmpeg_exe, clips, output, work_dir)

    if vertical:
        from highlights.video.vertical import make_vertical

        vertical_path = output.with_name(f"{output.stem}-vertical{output.suffix}")
        with console.status("Writing the vertical version..."):
            make_vertical(tools.ffmpeg_exe, output, vertical_path, vertical, vertical_killfeed, config.recording.crf)
        info(f"Vertical version: {vertical_path}")

    # Now that real clip lengths are known, give each kill a time in the final video.
    from highlights.video.concat import FADE_DURATION
    from highlights.video.ffmpeg import probe_duration

    finalize_report(
        report_path,
        {o.sequence.number: probe_duration(tools.ffmpeg_exe, c) for o, c in zip(outputs, clips)},
        overlap_seconds=FADE_DURATION if transitions == "fade" else 0.0,
        offset_seconds=timing_offset,
    )

    if not keep_clips:
        shutil.rmtree(work_dir, ignore_errors=True)
    else:
        info(f"Individual clips kept in {work_dir}")

    elapsed = int(time.monotonic() - started)
    console.print(
        f"[green]Done:[/green] {output} "
        f"({len(clips)} clips, {total:.0f}s of highlights, took {elapsed}s)"
    )


def _apply_slowmo(ffmpeg_exe: Path, recorded: list, clips_dir: Path, crf: int) -> list:
    """Slow the planned window of each POV clip that has one."""
    from highlights.video.slowmo import slow_clip

    result = []
    for output, clip in recorded:
        plan = output.sequence.slowmo
        if plan:
            clip = slow_clip(
                ffmpeg_exe,
                clip,
                clips_dir / f"slow_{output.sequence.number:02d}.mp4",
                plan["start_seconds"],
                plan["end_seconds"],
                plan["speed"],
                crf,
            )
        result.append((output, clip))
    return result


def _splice_cinematics(
    ffmpeg_exe: Path, recorded: list, clips_dir: Path, crf: int, size: tuple[int, int] = (1920, 1080)
) -> list:
    """Replace each POV clip with its spliced version; spliced cinematic clips leave the list.

    Takes of one play are overlaid in order (cam path first, generated cams on top).
    A cinematic whose POV clip failed to record is dropped; a POV clip whose
    cinematic failed stays as it was. Append-mode takes (no segments) stay in the list."""
    from highlights.video.splice import splice_clip

    takes: dict[int, list] = {}
    for output, clip in recorded:
        if output.sequence.cinematic_of is not None and output.sequence.splice_segments:
            takes.setdefault(output.sequence.cinematic_of, []).append((output, clip))
    result = []
    for output, clip in recorded:
        sequence = output.sequence
        if sequence.cinematic_of is not None and sequence.splice_segments:
            continue
        ordered = sorted(takes.get(sequence.number, []), key=lambda take: take[0].sequence.dynamic_cam is not None)
        for step, (take, take_clip) in enumerate(ordered):
            kind = "generated cam" if take.sequence.dynamic_cam is not None else "cam path"
            info(
                f"Splicing {kind} into clip {sequence.number}"
                + (" with tilt-shift" if take.sequence.tilt else "")
                + f" ({step + 1}/{len(ordered)})..."
            )
            clip = splice_clip(
                ffmpeg_exe,
                clip,
                take_clip,
                take.sequence.splice_segments,
                clips_dir / f"spliced_{sequence.number:02d}_{step}.mp4",
                crf,
                tilt=size if take.sequence.tilt else None,
            )
        result.append((output, clip))
    return result


@main.command()
@click.argument("demos", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--player", default=None, help="Only this player's highlights (steamid64 or name)")
@click.option(
    "--players",
    "players",
    multiple=True,
    help="Only these players' highlights: comma-separated and/or repeated (steamid64 or name)",
)
@click.option("--top", type=int, default=None, help="Max number of highlights")
@click.option("--max-duration", type=float, default=None, help="Max total video length, seconds")
@click.option(
    "--pick",
    "picks",
    multiple=True,
    metavar="1,2,4",
    help="Keep only these rows of the table this command prints without --pick (numbers or ranges like 3-6)",
)
@click.option("--exclude", "excludes", multiple=True, metavar="3,7", help="Drop these rows of that table")
@click.option(
    "--round",
    "rounds",
    multiple=True,
    metavar="N|DEMO:N",
    help="Only plays from these rounds (as numbered in the table): 13, 13,15 or semi-finals:13 for one "
    "demo; repeatable. Applied before ranking, so --top counts only these",
)
@click.option("--min-kills", type=int, default=None, help="Min kills for a moment to qualify")
@click.option(
    "--slow-mode",
    type=click.Choice(["off", "auto"]),
    default="off",
    help="auto: also show which of these clips would get a slow-motion beat, and on which kill",
)
def top(
    demos: tuple[Path, ...],
    player: str | None,
    players: tuple[str, ...],
    top: int | None,
    max_duration: float | None,
    min_kills: int | None,
    slow_mode: str,
    rounds: tuple[str, ...],
    picks: tuple[str, ...],
    excludes: tuple[str, ...],
) -> None:
    """Rank highlights across several DEMOs and print one table (no recording)."""
    from highlights.analysis.selection import apply_picks, parse_numbers, parse_rounds, round_allowed

    try:
        round_rules = parse_rounds(rounds)
        pick_numbers, exclude_numbers = parse_numbers(picks), parse_numbers(excludes)
    except ValueError as exc:
        raise click.UsageError(str(exc))
    config = load_config()
    for section, name, value in (
        (config.selection, "top", top),
        (config.selection, "max_duration", max_duration),
        (config.selection, "min_kills", min_kills),
    ):
        if value is not None:
            setattr(section, name, value)

    queries = [*([player] if player else []), *players]

    rows: list[tuple[DemoData, Moment]] = []
    failed = 0
    for demo_path in demos:
        work_dir = _resolve_work_dir(config, demo_path)
        with console.status(f"Parsing {demo_path.name}..."):
            parsed_path = ensure_dem_file(demo_path, work_dir)
            try:
                parsed = parse_demo(parsed_path)
            except DemoParseError as exc:
                warn(f"{exc} — skipping this demo")
                failed += 1
                continue
        info(
            f"Parsed [bold]{demo_path.name}[/bold] ({parsed.map_name}): {len(parsed.rounds)} rounds, "
            f"{len(parsed.kills)} kills"
        )

        moments = _scored_moments(parsed, config)

        player_steamids: list[str] | None = None
        if queries:
            try:
                player_steamids = resolve_players(parsed, queries)
            except PlayerNotFound as exc:
                warn(f"{demo_path.name}: {exc} — skipping this demo for these players")
                continue

        for moment in eligible_moments(moments, config.selection, player_steamids):
            if round_allowed(round_rules, demo_path.stem, moment.round_index):
                rows.append((parsed, moment))

    if failed == len(demos):
        raise click.ClickException("None of the demos could be read")

    rows, total_seconds = pick_rows(rows, config.selection, config.recording)
    numbers = None
    if rows and (pick_numbers or exclude_numbers):
        try:
            kept = apply_picks(rows, pick_numbers, exclude_numbers)
        except ValueError as exc:
            raise click.UsageError(str(exc))
        numbers, rows = [n for n, _ in kept], [row for _, row in kept]
        total_seconds = sum(clip_seconds(m, d, config.recording) for d, m in rows)

    if not rows:
        if config.selection.max_duration > 0:
            raise click.ClickException(
                f"No clip fits in --max-duration {config.selection.max_duration:.0f}s — raise it"
            )
        raise click.ClickException(
            "No highlights matched the criteria — try --min-kills 1 or check the demos"
        )

    slowmo_cells = None
    if slow_mode == "auto":
        from highlights.analysis.slowmo import best_kill, choose

        picks = choose([best_kill(moment) for _, moment in rows])
        slowmo_cells = [
            "" if pick is None else f"kill {sorted(k.tick for k in moment.kills).index(pick.kill.tick) + 1}/"
            f"{moment.kill_count}: {pick.reason}"
            for (_, moment), pick in zip(rows, picks)
        ]
    print_top_table(rows, slowmo_cells, numbers=numbers)
    if slowmo_cells is not None:
        info(f"Slow-motion for {sum(1 for c in slowmo_cells if c)} of {len(rows)} clips")
    if config.selection.max_duration > 0:
        info(f"{len(rows)} clips, about {total_seconds:.0f}s of {config.selection.max_duration:.0f}s")


@main.command()
@click.argument("demos", nargs=-1, required=True, metavar="DEMO...")
@click.option(
    "--players",
    "players",
    multiple=True,
    help="Only these players' highlights: comma-separated and/or repeated (steamid64 or name); "
    "players missing from a demo are fine. Without --players/--team everyone is eligible",
)
@click.option(
    "--team",
    "teams",
    multiple=True,
    help="Add every player of this team from --names-file (repeatable)",
)
@click.option(
    "--names-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="A names.json with steamid64 -> {name, team} (the lanchamp-cotw format), used by --team",
)
@click.option("--top", type=int, default=None, help="How many clips in total, best first across all demos")
@click.option("--max-duration", type=float, default=None, help="Max summed clip length, seconds")
@click.option(
    "--pick",
    "picks",
    multiple=True,
    metavar="1,2,4",
    help="Keep only these rows of the table this command prints without --pick (numbers or ranges like 3-6)",
)
@click.option("--exclude", "excludes", multiple=True, metavar="3,7", help="Drop these rows of that table")
@click.option(
    "--round",
    "rounds",
    multiple=True,
    metavar="N|DEMO:N",
    help="Only plays from these rounds (as numbered in the table): 13, 13,15 or semi-finals:13 for one "
    "demo; repeatable. Applied before ranking, so --top counts only these",
)
@click.option("--min-kills", type=int, default=None, help="Min kills for a moment to qualify")
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("highlights-out"),
    show_default=True,
    help="Where <label>-<player>-<n>.mp4 and .json are written",
)
@click.option("--fps", type=int, default=None, help="Recording framerate")
@click.option("--width", type=int, default=None, help="Recording width")
@click.option("--height", type=int, default=None, help="Recording height")
@click.option("--crf", type=int, default=None, help="x264 CRF for recorded clips")
@click.option(
    "--player-name",
    "player_names",
    multiple=True,
    metavar="STEAMID=NAME",
    help="Rename a player in the recording (steamid64=Display Name); repeatable, overrides config.toml",
)
@click.option(
    "--cmd",
    "extra_commands",
    multiple=True,
    metavar="COMMAND",
    help="Extra console command run at every clip's start (repeatable)",
)
@click.option("--cinematic", is_flag=True, help="Cut your cam paths into each clip (splice mode)")
@click.option("--cinematic-path", default=None, help="Use this cam path for every clip instead of matching")
@click.option(
    "--tilt-mode",
    type=click.Choice(["off", "auto", "on"]),
    default="off",
    help="Tilt-shift look on cam-path shots (needs --cinematic): auto = wide, elevated shots only, on = all",
)
@click.option("--campath-hold", is_flag=True, help="Keep each cam path's last pose until the clip ends")
@click.option("--campath-align", is_flag=True, help="Also start each cam path late, where it sees the player longest")
@click.option(
    "--intro-cam",
    default=None,
    callback=lambda ctx, param, value: _parse_cam_styles(value),
    help="Open every clip with a generated cam: face, profile, gun, tps, heli, a list, or random",
)
@click.option(
    "--downtime-cam",
    default=None,
    callback=lambda ctx, param, value: _parse_cam_styles(value),
    help="Cut a generated cam into the down time between kills (same styles as --intro-cam)",
)
@click.option("--json-only", is_flag=True, help="Select and plan only: print the table, write the JSON files, record nothing")
@click.option("--keep-clips", is_flag=True, help="Keep each demo's work dir with the raw clips")
@click.option(
    "--buildup",
    type=click.Choice(["empty", "players"]),
    default=None,
    help="Also record a cam-path build-up shot per clip from the same round before the play "
    "(<name>-buildup.mp4): empty = nobody in view, players = players in view",
)
@click.option("--buildup-length", type=float, default=6.0, show_default=True, help="Longest build-up shot, seconds")
@click.option("--timing-offset", type=float, default=0.0, help="Seconds added to kill times in the reports")
def batch(
    demos: tuple[str, ...],
    players: tuple[str, ...],
    teams: tuple[str, ...],
    names_file: Path | None,
    top: int | None,
    max_duration: float | None,
    min_kills: int | None,
    rounds: tuple[str, ...],
    picks: tuple[str, ...],
    excludes: tuple[str, ...],
    output_dir: Path,
    fps: int | None,
    width: int | None,
    height: int | None,
    crf: int | None,
    player_names: tuple[str, ...],
    extra_commands: tuple[str, ...],
    cinematic: bool,
    cinematic_path: str | None,
    tilt_mode: str,
    campath_hold: bool,
    campath_align: bool,
    intro_cam: list[str] | None,
    downtime_cam: list[str] | None,
    json_only: bool,
    keep_clips: bool,
    timing_offset: float,
    buildup: str | None,
    buildup_length: float,
) -> None:
    """One clip + JSON per highlight across DEMOs, for a separate compilation step.

    DEMO is PATH or LABEL=PATH (the label names the files). The best --top moments of all
    demos (optionally only the given players) are picked, then every demo is recorded once
    and each moment is written as <label>-<player>-<n>.mp4 with its own .json report.
    """
    from highlights.analysis.selection import parse_numbers, parse_rounds
    from highlights.batch import DemoJob, parse_demo_arg, run_batch, team_steamids

    try:
        round_rules = parse_rounds(rounds)
        pick_numbers, exclude_numbers = parse_numbers(picks), parse_numbers(excludes)
    except ValueError as exc:
        raise click.UsageError(str(exc))

    if teams and names_file is None:
        raise click.UsageError("--team needs --names-file")
    if (campath_hold or campath_align or cinematic_path) and not cinematic:
        raise click.UsageError("--campath-hold/--campath-align/--cinematic-path need --cinematic")
    if tilt_mode != "off" and not cinematic:
        raise click.UsageError("--tilt-mode needs --cinematic")
    config = load_config()
    for section, name, value in (
        (config.selection, "top", top),
        (config.selection, "max_duration", max_duration),
        (config.selection, "min_kills", min_kills),
        (config.recording, "fps", fps),
        (config.recording, "width", width),
        (config.recording, "height", height),
        (config.recording, "crf", crf),
    ):
        if value is not None:
            setattr(section, name, value)
    config.recording.player_names.update(_parse_player_names(player_names))
    config.recording.extra_commands = [*config.recording.extra_commands, *extra_commands]

    team_ids: set[str] = set()
    if teams:
        team_ids = team_steamids(names_file, list(teams))
        info(f"Team filter: {len(team_ids)} player(s) from {names_file.name} for {', '.join(teams)}")
        if not team_ids:
            raise click.ClickException(f"No player in {names_file} has team {', '.join(teams)}")

    jobs = []
    seen: set[str] = set()
    for value in demos:
        label, path = parse_demo_arg(value)
        if not path.is_file():
            raise click.UsageError(f"Demo not found: {path}")
        if label in seen:
            raise click.UsageError(f"Two demos share the label {label!r}; name them LABEL=PATH")
        seen.add(label)
        work_dir = _resolve_work_dir(config, path)
        work_dir.mkdir(parents=True, exist_ok=True)
        with console.status(f"Parsing {label}..."):
            try:
                parsed = parse_demo(ensure_dem_file(path, work_dir))
            except DemoParseError as exc:
                warn(f"{label}: {exc} — skipping this demo")
                continue
        info(f"Parsed [bold]{label}[/bold] ({parsed.map_name}): {len(parsed.rounds)} rounds, {len(parsed.kills)} kills")
        jobs.append(DemoJob(label, path, parsed, work_dir))
    if not jobs:
        raise click.ClickException("None of the demos could be read")

    started = time.monotonic()
    written = run_batch(
        jobs,
        config,
        [*players],
        team_ids,
        output_dir,
        json_only=json_only,
        keep_clips=keep_clips,
        timing_offset=timing_offset,
        cinematic=cinematic,
        cinematic_path=cinematic_path,
        hold=campath_hold or campath_align,
        align=campath_align,
        intro_cam=intro_cam,
        downtime_cam=downtime_cam,
        tilt_mode=tilt_mode,
        round_rules=round_rules,
        pick=pick_numbers,
        exclude=exclude_numbers,
        buildup=buildup,
        buildup_length=buildup_length,
    )
    if json_only:
        console.print(f"[green]Planned:[/green] reports written to {output_dir}")
    else:
        console.print(
            f"[green]Done:[/green] {len(written)} clip(s) in {output_dir} (took {int(time.monotonic() - started)}s)"
        )


@main.command()
@click.argument("demos", nargs=-1, required=True)
@click.option(
    "--mode",
    type=click.Choice(["empty", "players"]),
    required=True,
    help="empty: cam paths with nobody in view (establishing shots); players: cam paths with random "
    "players from the demo in view",
)
@click.option("--count", type=click.IntRange(min=1), default=3, show_default=True, help="Clips per demo")
@click.option("--max-length", type=float, default=8.0, show_default=True, help="Longest clip, seconds")
@click.option("--seed", type=int, default=None, help="Repeat an earlier random pick")
@click.option("--cinematic-path", default=None, help="Only use this cam path (file name without extension)")
@click.option("-o", "--output-dir", type=click.Path(file_okay=False, path_type=Path), default=Path("broll-out"))
@click.option("--fps", type=int, default=None, help="Recording framerate")
@click.option("--width", type=int, default=None, help="Recording width")
@click.option("--height", type=int, default=None, help="Recording height")
@click.option("--crf", type=int, default=None, help="x264 CRF for recorded clips")
@click.option("--cmd", "extra_commands", multiple=True, metavar="COMMAND", help="Extra console command run at every clip's start")
@click.option("--json-only", is_flag=True, help="Plan and write the JSON files only, do not record")
@click.option("--keep-clips", is_flag=True, help="Keep the temporary work files")
def broll(
    demos: tuple[str, ...],
    mode: str,
    count: int,
    max_length: float,
    seed: int | None,
    cinematic_path: str | None,
    output_dir: Path,
    fps: int | None,
    width: int | None,
    height: int | None,
    crf: int | None,
    extra_commands: tuple[str, ...],
    json_only: bool,
    keep_clips: bool,
) -> None:
    """Cam-path-only B-roll clips from DEMOs (PATH or LABEL=PATH), for pre-edit build-up.

    Records your cam paths over random stretches of live play and writes
    <label>-broll-<mode>-<n>.mp4 + .json per clip. Needs cam paths for the map.
    """
    from highlights.batch import DemoJob, parse_demo_arg
    from highlights.broll import run_broll
    from highlights.recording.campaths import load_campaths, resolve_campaths_dir

    config = load_config()
    for name, value in (("fps", fps), ("width", width), ("height", height), ("crf", crf)):
        if value is not None:
            setattr(config.recording, name, value)
    config.recording.extra_commands = [*config.recording.extra_commands, *extra_commands]

    jobs = []
    seen: set[str] = set()
    for value in demos:
        label, path = parse_demo_arg(value)
        if not path.is_file():
            raise click.UsageError(f"Demo not found: {path}")
        if label in seen:
            raise click.UsageError(f"Two demos share the label {label!r}; name them LABEL=PATH")
        seen.add(label)
        work_dir = _resolve_work_dir(config, path)
        work_dir.mkdir(parents=True, exist_ok=True)
        with console.status(f"Parsing {label}..."):
            try:
                parsed = parse_demo(ensure_dem_file(path, work_dir))
            except DemoParseError as exc:
                warn(f"{label}: {exc} — skipping this demo")
                continue
        jobs.append(DemoJob(label, path, parsed, work_dir))
    if not jobs:
        raise click.ClickException("None of the demos could be read")

    campaths_dir = resolve_campaths_dir(config)

    def campaths_for(map_name: str) -> list:
        paths = load_campaths(campaths_dir, map_name)
        if cinematic_path:
            paths = [p for p in paths if p.name.lower() == cinematic_path.lower()]
        if not paths:
            warn(f"No cam paths for {map_name} in {campaths_dir}" + (f" named {cinematic_path!r}" if cinematic_path else ""))
        return paths

    started = time.monotonic()
    written = run_broll(
        jobs, config, campaths_for, mode, count, max_length, output_dir, seed, json_only=json_only, keep_clips=keep_clips
    )
    if json_only:
        console.print(f"[green]Planned:[/green] reports written to {output_dir}")
    else:
        console.print(f"[green]Done:[/green] {len(written)} clip(s) in {output_dir} (took {int(time.monotonic() - started)}s)")


@main.group(name="places")
def places_group() -> None:
    """Build and inspect the per-map place lookup used to describe where plays happen."""


@places_group.command()
@click.argument("demos", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--every", type=float, default=1.0, help="Sample every N seconds of demo time")
def build(demos: tuple[Path, ...], every: float) -> None:
    """Sample place names from DEMOs and accumulate them into the per-map lookup."""
    if every <= 0:
        raise click.BadParameter("must be > 0", param_hint="--every")

    config = load_config()
    maps: dict[str, PlaceMap] = {}
    failed = 0
    for demo_path in demos:
        work_dir = _resolve_work_dir(config, demo_path)
        with console.status(f"Parsing {demo_path.name}..."):
            parsed_path = ensure_dem_file(demo_path, work_dir)
            try:
                parsed = parse_demo(parsed_path)
            except DemoParseError as exc:
                warn(f"{exc} — skipping this demo")
                failed += 1
                continue
        step = max(1, round(parsed.tickrate * every))
        ticks = list(range(1, parsed.last_tick + 1, step))
        try:
            samples = sample_places(parsed_path, ticks)
        except DemoParseError as exc:
            warn(f"{exc} — skipping this demo")
            failed += 1
            continue
        info(f"[bold]{demo_path.name}[/bold] ({parsed.map_name}): {len(samples)} place samples")
        if not samples:
            warn(f"{demo_path.name}: no place samples found; skipping")
            continue
        place_map = maps.get(parsed.map_name)
        if place_map is None:
            place_map = maps[parsed.map_name] = load_places(parsed.map_name) or PlaceMap(parsed.map_name)
        place_map.add_many(samples)

    if failed == len(demos):
        raise click.ClickException("None of the demos could be read")

    for place_map in maps.values():
        path = save_places(place_map)
        info(f"Saved [bold]{place_map.map_name}[/bold]: {len(place_map)} cells -> {path}")


@places_group.command(name="list")
def list_places() -> None:
    """Show the per-map place lookups saved on this machine."""
    from rich.table import Table

    directory = places_dir()
    rows = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        place_map = load_places(path.stem)
        if place_map is None:
            continue
        rows.append((place_map.map_name, len(place_map)))

    if not rows:
        info("No place maps yet — run [bold]highlights places build DEMO...[/bold] to create one.")
        return

    table = Table(title="Saved place maps")
    table.add_column("Map")
    table.add_column("Cells", justify="right")
    for map_name, cells in rows:
        table.add_row(map_name, str(cells))
    console.print(table)


@main.command()
def doctor() -> None:
    """Check that everything needed for recording is in place."""
    from rich.table import Table

    from highlights.recording import steam, toolchain

    config = load_config()
    table = Table(title="cs2-highlights doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")

    def row(name: str, ok: bool | None, detail: str) -> None:
        status = "[green]OK[/green]" if ok else ("[yellow]--[/yellow]" if ok is None else "[red]FAIL[/red]")
        table.add_row(name, status, detail)

    steam_folder = steam.get_steam_folder()
    row("Steam installed", steam_folder is not None, str(steam_folder or "registry key not found"))

    cs2_folder = steam.get_cs2_folder()
    if config.paths.cs2_exe != "auto":
        cs2_exe = Path(config.paths.cs2_exe)
    else:
        cs2_exe = steam.get_cs2_exe(cs2_folder) if cs2_folder else None
    row(
        "CS2 installed",
        cs2_exe is not None and cs2_exe.is_file(),
        str(cs2_exe or "app 730 not found in Steam library"),
    )

    row(
        "Steam running",
        steam.is_steam_running(),
        "required at recording time" if not steam.is_steam_running() else "",
    )

    hlae = toolchain.find_hlae(config)
    row("HLAE", True if hlae else None, str(hlae or "missing — will be auto-downloaded on first run"))

    if hlae and cs2_folder:
        from datetime import datetime, timezone

        inf = steam.read_steam_inf(cs2_folder)
        hlae_info = toolchain.read_hlae_install_info(hlae)
        cs2_date = hlae_date = None
        try:
            cs2_date = datetime.strptime(inf.get("VersionDate", ""), "%b %d %Y").replace(tzinfo=timezone.utc)
            hlae_date = datetime.fromisoformat(hlae_info.get("published_at", "").replace("Z", "+00:00"))
        except ValueError:
            pass
        if cs2_date and hlae_date:
            compatible = hlae_date >= cs2_date
            row(
                "HLAE vs CS2 build",
                True if compatible else False,
                f"CS2 {inf.get('PatchVersion', '?')} from {cs2_date:%Y-%m-%d}, "
                f"HLAE {hlae_info.get('tag', '?')} from {hlae_date:%Y-%m-%d}"
                + ("" if compatible else " — CS2 is newer, injection will likely crash; run `highlights update-tools` once advancedfx releases an update"),
            )

    ffmpeg = toolchain.find_ffmpeg(config)
    row("FFmpeg", True if ffmpeg else None, str(ffmpeg or "missing — will be auto-downloaded on first run"))

    plugin_dll = toolchain.find_plugin_dll(config)
    row(
        "CS2 server plugin",
        plugin_dll is not None,
        str(plugin_dll or "install CS Demo Manager or put server.dll in vendor/cs2-plugin/"),
    )

    from highlights.recording.campaths import resolve_campaths_dir

    campaths_dir = resolve_campaths_dir(config)
    campath_files = (
        sum(1 for f in campaths_dir.rglob("*") if f.is_file() and f.suffix.lower() in {".dz", ".campath", ".xml"})
        if campaths_dir.is_dir()
        else 0
    )
    row(
        "Cinematic cam paths",
        True if campath_files else None,
        f"{campath_files} file(s) in {campaths_dir}" if campath_files else f"none in {campaths_dir} (optional, for --cinematic)",
    )

    from highlights.config import app_data_dir

    work_base = app_data_dir()
    work_base.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(work_base).free / (1 << 30)
    row("Disk space (work dir)", free_gb > 5, f"{free_gb:.1f} GB free at {work_base}")

    console.print(table)


@main.command(name="update-tools")
def update_tools() -> None:
    """Re-download the latest HLAE and FFmpeg (e.g. after a CS2 update broke HLAE)."""
    from highlights.recording import toolchain

    hlae = toolchain.install_hlae()
    ffmpeg = toolchain.install_ffmpeg()
    toolchain.write_ffmpeg_ini(hlae, ffmpeg)
    console.print("[green]Tools updated.[/green] Run `highlights doctor` to verify.")


@main.command(hidden=True, name="record-one")
@click.argument("demo", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--start-tick", type=int, required=True)
@click.option("--end-tick", type=int, required=True)
@click.option("--steamid", required=True, help="Player to spectate (steamid64)")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None)
def record_one(demo: Path, start_tick: int, end_tick: int, steamid: str, output: Path | None) -> None:
    """Recording proof of concept: record a single arbitrary clip from a demo."""
    from highlights.recording.recorder import record, resolve_tools
    from highlights.video.postprocess import process_clip

    config = load_config()
    work_dir = _resolve_work_dir(config, demo)
    work_dir.mkdir(parents=True, exist_ok=True)

    with console.status("Parsing demo..."):
        demo_path = ensure_dem_file(demo, work_dir)
        parsed = parse_demo(demo_path)

    if steamid not in parsed.slots:
        warn(f"steamid {steamid} has no known slot; known: {parsed.slots}")

    sequence = Sequence(
        number=1,
        start_tick=start_tick,
        end_tick=end_tick,
        cameras=[CameraAction(tick=start_tick, steamid=steamid)],
        label=f"PoC clip {start_tick}-{end_tick}",
        score=0.0,
        round_index=0,
    )
    outputs = record(parsed, [sequence], config, work_dir)
    if not outputs:
        raise click.ClickException("No clip was recorded")

    tools = resolve_tools(config, auto_install=False)
    clip = process_clip(tools.ffmpeg_exe, outputs[0], work_dir / "clips", overlay=False)
    final = output or demo.with_name(demo.stem + "-poc.mp4")
    shutil.copyfile(clip, final)
    console.print(f"[green]PoC clip:[/green] {final}")


if __name__ == "__main__":
    main()
