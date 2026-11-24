"""Click commands: `highlights broadcast-sync` and `highlights broadcast-anchor`."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

import click
from rich.table import Table

from highlights.broadcast.anchors import (
    EVENTS,
    add_anchor,
    event_tick,
    format_timestamp,
    load_anchor_file,
    parse_timestamp,
    save_anchor_file,
)
from highlights.broadcast.audio import probe_media
from highlights.broadcast.export import export_clip, sidecar_path
from highlights.broadcast.matching import MAX_ANCHOR_DISTANCE, MODES, plan_report
from highlights.broadcast.models import Anchor, AnchorFile, BroadcastSyncError, ClipPlan
from highlights.broadcast.reports import (
    LoadedReport,
    check_against_demo,
    load_report,
    not_a_highlight_report,
    same_demo_name,
)
from highlights.broadcast.timeline import DRIFT_TOLERANCE, Timeline
from highlights.broadcast.voice import DEVICES as VOICE_DEVICES
from highlights.broadcast.voice import require as require_voice
from highlights.config import load_config
from highlights.demo.parser import DemoParseError, parse_demo
from highlights.util.log import console, info, warn

SIDECAR_SUFFIX = "-broadcast-sync.json"


def _fail(exc: BroadcastSyncError) -> click.ClickException:
    return click.ClickException(str(exc))


def _parse(demo_path: Path):
    with console.status("Parsing demo..."):
        try:
            return parse_demo(demo_path)
        except DemoParseError as exc:
            raise click.ClickException(str(exc)) from None


def _find_ffmpeg() -> Path | None:
    from highlights.recording import toolchain

    return toolchain.find_ffmpeg(load_config())


def _same_name(recorded: str, demo_path: Path) -> bool:
    return PureWindowsPath(recorded).name.casefold() == demo_path.name.casefold()


def _collect_reports(reports: tuple[Path, ...], reports_dir: Path | None, demo_path: Path, skip: Path) -> list[LoadedReport]:
    loaded: dict[Path, LoadedReport] = {}
    try:
        for path in reports:
            loaded[path.resolve()] = load_report(path)
    except BroadcastSyncError as exc:
        raise _fail(exc) from None
    if reports_dir is not None:
        other_demos = unreadable = 0
        for path in sorted(reports_dir.glob("*.json")):
            if path.name.endswith(SIDECAR_SUFFIX) or path.resolve() in (skip.resolve(), *loaded):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                unreadable += 1
                continue
            if not_a_highlight_report(data):
                continue
            try:
                report = load_report(path)
            except BroadcastSyncError as exc:
                warn(str(exc))
                continue
            if not same_demo_name(report, demo_path):
                other_demos += 1
                continue
            loaded[path.resolve()] = report
        if other_demos:
            info(f"Skipped {other_demos} report(s) in {reports_dir} made from other demos")
        if unreadable:
            warn(f"Skipped {unreadable} unreadable .json file(s) in {reports_dir}")
    if not loaded:
        where = f" in {reports_dir}" if reports_dir else ""
        raise click.ClickException(f"No highlight reports for {demo_path.name}{where}")
    return list(loaded.values())


@click.command(name="broadcast-sync")
@click.option("--demo", "demo_path", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path), help="The .dem the highlight reports came from")
@click.option("--vod", type=click.Path(path_type=Path), default=None, help="Broadcast video (any file FFmpeg reads); default: the anchor file's `vod`, relative to the anchor file")
@click.option("--anchors", "anchors_path", required=True, type=click.Path(path_type=Path), help="Anchor file (create it with `highlights broadcast-anchor`)")
@click.option("--report", "reports", multiple=True, type=click.Path(path_type=Path), help="A highlight .json report (repeatable): `run` output (also --json-only) or one batch clip")
@click.option("--reports-dir", type=click.Path(exists=True, file_okay=False, path_type=Path), default=None, help="Use every highlight report in this folder that was made from --demo")
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path), default=None, help="Where the WAV + sidecar go (default: beside each report)")
@click.option("--mode", type=click.Choice(MODES), default="highlight", show_default=True, help="highlight: the clip plus padding; round: the whole round plus padding")
@click.option("--lead-in", type=click.FloatRange(min=0), default=5.0, show_default=True, help="Seconds of build-up before the clip (or round)")
@click.option("--lead-out", type=click.FloatRange(min=0), default=8.0, show_default=True, help="Seconds of reaction after the clip (or round)")
@click.option("--dry-run", is_flag=True, help="Show the planned segments only; write nothing")
@click.option("--allow-review", is_flag=True, help="Also export `needs_review` clips (their sidecar still says so)")
@click.option("--strict", is_flag=True, help="Exit with an error when any clip is not `matched`")
@click.option("--audio-stream", type=click.IntRange(min=0), default=0, show_default=True, help="Which audio stream of the VOD to cut (0 = first)")
@click.option("--drift-tolerance", type=click.FloatRange(min=0), default=DRIFT_TOLERANCE, show_default=True, help="Seconds two anchors of one segment may disagree by")
@click.option("--max-anchor-distance", type=click.FloatRange(min=0), default=MAX_ANCHOR_DISTANCE, show_default=True, help="Game seconds a clip may be from the nearest anchor and still count as `matched`")
@click.option("--isolate-voice", is_flag=True, help="Also write <name>-broadcast-voice.wav: the commentary alone, without game sound and music (needs `pip install \"cs2-highlights[voice]\"`)")
@click.option("--voice-device", type=click.Choice(VOICE_DEVICES), default="auto", show_default=True, help="Where --isolate-voice runs (auto = cuda, then mps, then cpu)")
def broadcast_sync(
    demo_path: Path,
    vod: Path | None,
    anchors_path: Path,
    reports: tuple[Path, ...],
    reports_dir: Path | None,
    output_dir: Path | None,
    mode: str,
    lead_in: float,
    lead_out: float,
    dry_run: bool,
    allow_review: bool,
    strict: bool,
    audio_stream: int,
    drift_tolerance: float,
    max_anchor_distance: float,
    isolate_voice: bool,
    voice_device: str,
) -> None:
    """Cut the broadcast commentary for each highlight into a WAV + sync JSON sidecar.

    Needs the demo, the broadcast VOD, highlight reports (from `run`, `run --json-only`
    or `batch`) and an anchor file saying where known demo moments are in the VOD.
    Nothing is modified: only <name>-broadcast.wav and <name>-broadcast-sync.json are written
    (plus <name>-broadcast-voice.wav with --isolate-voice).
    Relative paths resolve against the current folder, except the anchor file's `vod`,
    which resolves against the anchor file's folder.
    """
    if strict and allow_review:
        raise click.UsageError("--strict and --allow-review contradict each other")
    if not reports and reports_dir is None:
        raise click.UsageError("Give --report (repeatable) or --reports-dir")

    try:
        if isolate_voice and not dry_run:
            require_voice()
        anchors = load_anchor_file(anchors_path)
        if not _same_name(anchors.demo, demo_path):
            raise BroadcastSyncError(f"{anchors_path.name} is for demo {anchors.demo!r}, not {demo_path.name!r}")
        vod_path = vod
        if vod_path is None and anchors.vod:
            vod_path = anchors_path.parent / anchors.vod
        if vod_path is None and not dry_run:
            raise BroadcastSyncError("No VOD: pass --vod or set `vod` in the anchor file")

        loaded = _collect_reports(reports, reports_dir, demo_path, anchors_path)
        demo = _parse(demo_path)
        info(f"Parsed [bold]{demo.map_name}[/bold]: {len(demo.rounds)} rounds, tickrate {float(demo.tickrate):g}")
        for report in loaded:
            check_against_demo(report, demo, demo_path)
        timeline = Timeline(demo, anchors, drift_tolerance)
        for segment_id, anchor in timeline.ignored:
            warn(f"segment {segment_id!r}: ignoring unverified anchor at tick {anchor.demo_tick}")

        ffmpeg = _find_ffmpeg()
        vod_duration = None
        if dry_run and (vod_path is None or ffmpeg is None or not vod_path.is_file()):
            warn(f"VOD or ffmpeg unavailable ({vod_path}); dry run cannot check the clips against the VOD length")
        else:
            if ffmpeg is None:
                raise BroadcastSyncError("ffmpeg not found; install it or run `highlights update-tools`")
            media = probe_media(ffmpeg, vod_path)
            if audio_stream >= len(media.audio_streams):
                raise BroadcastSyncError(
                    f"{vod_path.name} has {len(media.audio_streams)} audio stream(s); --audio-stream {audio_stream} does not exist"
                )
            vod_duration = media.duration
            info(f"VOD {vod_path.name}: {format_timestamp(vod_duration)}, {len(media.audio_streams)} audio stream(s)")

        plans: list[tuple[LoadedReport, ClipPlan]] = []
        for report in loaded:
            plans += [
                (report, plan)
                for plan in plan_report(timeline, report, mode, lead_in, lead_out, vod_duration, max_anchor_distance)
            ]
        _print_plans(plans)

        counts = {s: sum(1 for _, p in plans if p.status == s) for s in ("matched", "needs_review", "unmatched")}
        summary = ", ".join(f"{n} {name}" for name, n in counts.items())
        if dry_run:
            info(f"Dry run: {summary}. Nothing written.")
        else:
            for report, plan in plans:
                target_dir = output_dir or report.path.parent
                export_clip(
                    ffmpeg,
                    vod_path,
                    target_dir,
                    plan,
                    report,
                    allow_review,
                    audio_stream,
                    voice_device=voice_device if isolate_voice else None,
                    tickrate=float(demo.tickrate),
                    map_name=demo.map_name,
                    demo_name=demo_path.name,
                    vod_name=vod_path.name,
                    anchor_file=anchors_path.name,
                    mode=mode,
                    lead_in=lead_in,
                    lead_out=lead_out,
                )
                if plan.status != "matched":
                    info(f"[yellow]{sidecar_path(target_dir, plan).name}[/yellow]: {plan.status}: {'; '.join(plan.reasons)}")
            exported = counts["matched"] + (counts["needs_review"] if allow_review else 0)
            info(f"[green]Done:[/green] {summary}; {exported} audio file(s) written")
        if strict and counts["matched"] != len(plans):
            raise click.ClickException(f"--strict: {len(plans) - counts['matched']} clip(s) are not matched")
    except BroadcastSyncError as exc:
        raise _fail(exc) from None


def _print_plans(plans: list[tuple[LoadedReport, ClipPlan]]) -> None:
    table = Table(title="Broadcast audio plan")
    for column in ("Clip", "Round", "Status", "VOD window", "Audio length", "Notes"):
        table.add_column(column)
    colors = {"matched": "green", "needs_review": "yellow", "unmatched": "red"}
    for report, plan in plans:
        window = plan.window
        notes = plan.reasons + (list(window.clamps) if window else [])
        table.add_row(
            plan.stem,
            str(plan.round),
            f"[{colors[plan.status]}]{plan.status}[/{colors[plan.status]}]",
            f"{format_timestamp(window.vod_start)} - {format_timestamp(window.vod_end)}" if window else "-",
            f"{window.duration:.1f}s" if window else "-",
            "; ".join(notes),
        )
    console.print(table)


def _parse_rounds(text: str) -> tuple[int, int]:
    try:
        first, _, last = text.partition("-")
        return int(first), int(last or first)
    except ValueError:
        raise click.BadParameter("use A-B (e.g. 1-12) or a single round", param_hint="--rounds") from None


@click.command(name="broadcast-anchor")
@click.option("--demo", "demo_path", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path), help="The demo the anchor is for")
@click.option("--anchors", "anchors_path", required=True, type=click.Path(path_type=Path), help="Anchor file to create or add to")
@click.option("--round", "round_number", required=True, type=click.IntRange(min=1), help="Round number (1-based, as in the highlights table)")
@click.option("--event", required=True, type=click.Choice(EVENTS), help="What you saw in the VOD: round_start = freeze time begins, freeze_end = the round clock starts, round_end = the round is decided, kill = see --kill")
@click.option("--kill", type=click.IntRange(min=1), default=None, help="With --event kill: the Nth kill of the round, counting from 1")
@click.option("--vod-time", required=True, help="Where it happens in the VOD: HH:MM:SS.mmm, MM:SS or seconds")
@click.option("--segment", "segment_id", default="main", show_default=True, help="Continuous live segment this belongs to; start a new one after a pause, halftime or desk break")
@click.option("--rounds", "rounds_text", default=None, help="Rounds the segment covers, e.g. 1-12 (only when creating it; default: the whole demo)")
@click.option("--vod", type=click.Path(path_type=Path), default=None, help="VOD file name to store in a new anchor file")
@click.option("--match-id", default=None, help="Label to store in a new anchor file")
@click.option("--drift-tolerance", type=click.FloatRange(min=0), default=DRIFT_TOLERANCE, show_default=True)
def broadcast_anchor(
    demo_path: Path,
    anchors_path: Path,
    round_number: int,
    event: str,
    kill: int | None,
    vod_time: str,
    segment_id: str,
    rounds_text: str | None,
    vod: Path | None,
    match_id: str | None,
    drift_tolerance: float,
) -> None:
    """Add a verified anchor: 'this round event is at this time in the VOD'.

    Find a moment you can identify on screen (the round clock starting is easiest),
    note the VOD time, and name the round. The tick is looked up from the demo; the
    whole anchor file is re-checked first, so a conflicting anchor is refused.
    """
    try:
        rounds = _parse_rounds(rounds_text) if rounds_text else None
        seconds = parse_timestamp(vod_time)
        demo = _parse(demo_path)
        tick = event_tick(demo, round_number, event, kill)
        if anchors_path.exists():
            anchors = load_anchor_file(anchors_path)
            if not _same_name(anchors.demo, demo_path):
                raise BroadcastSyncError(f"{anchors_path.name} is for demo {anchors.demo!r}, not {demo_path.name!r}")
        else:
            anchors = AnchorFile(anchors_path, match_id, demo_path.name, str(vod) if vod else None, [])
        anchor = Anchor(demo_tick=tick, vod_seconds=seconds, event=event, source="manual", verified=True)
        updated = add_anchor(anchors, demo, segment_id, anchor, rounds, drift_tolerance)
        save_anchor_file(updated)
    except BroadcastSyncError as exc:
        raise _fail(exc) from None
    info(
        f"Anchor saved to {anchors_path}: segment {segment_id!r}, round {round_number} {event}"
        f"{f' #{kill}' if kill else ''} = tick {tick} at VOD {format_timestamp(seconds)}"
    )
