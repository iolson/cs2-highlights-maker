"""Read schema-v3 highlight reports (`run`, `top`-less, `batch`) and check them against a demo."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from highlights.broadcast.models import BroadcastSyncError
from highlights.demo.parser import DemoData

SCHEMA_VERSION = 3
TICK_SLACK = 2  # ticks a kill may sit outside its clip bounds (rounding)


@dataclass
class ReportSequence:
    number: int
    round: int  # 1-based
    start_tick: int
    end_tick: int
    kill_events: list[dict] = field(default_factory=list)
    kill_ticks: list[int] = field(default_factory=list)
    slowmo: dict | None = None
    clip_duration_seconds: float | None = None  # ffprobe length, only in finalized (recorded) reports


@dataclass
class LoadedReport:
    path: Path
    demo_name: str
    map_name: str
    tickrate: float
    rounds: int
    sequences: list[ReportSequence]  # POV clips only; cinematic takes share their ticks
    raw: dict

    @property
    def clip_path(self) -> Path | None:
        """The rendered clip beside a batch report, when there is one."""
        candidate = self.path.with_suffix(".mp4")
        return candidate if candidate.is_file() else None


def not_a_highlight_report(data: object) -> str | None:
    """Why this JSON is not a highlight report (None when it is one)."""
    if not isinstance(data, dict):
        return "not a JSON object"
    if data.get("kind") in ("broll", "buildup", "teaser"):
        return f"a {data['kind']} report, not a highlight report"
    if "sequences" not in data or "demo" not in data:
        return "no 'demo' and 'sequences' (not a highlight report)"
    if data.get("schema_version") != SCHEMA_VERSION:
        return f"schema_version {data.get('schema_version')!r}, expected {SCHEMA_VERSION}"
    return None


def load_report(path: Path) -> LoadedReport:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BroadcastSyncError(f"Report not found: {path}") from None
    except (OSError, ValueError) as exc:
        raise BroadcastSyncError(f"Cannot read report {path}: {exc}") from None
    why = not_a_highlight_report(data)
    if why:
        raise BroadcastSyncError(f"{path} is not a usable highlight report: {why}")

    sequences: list[ReportSequence] = []
    for item in data["sequences"]:
        if item.get("cinematic"):
            continue
        try:
            events = [e for e in item.get("events", []) if e.get("type") == "kill"]
            kill_ticks = sorted(
                {int(t) for m in item.get("moments", []) for t in m.get("kill_ticks", [])}
                | {int(e["tick"]) for e in events}
            )
            sequences.append(
                ReportSequence(
                    number=int(item["number"]),
                    round=int(item["round"]),
                    start_tick=int(item["start_tick"]),
                    end_tick=int(item["end_tick"]),
                    kill_events=events,
                    kill_ticks=kill_ticks,
                    slowmo=item.get("slowmo"),
                    clip_duration_seconds=item.get("clip_duration_seconds"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BroadcastSyncError(
                f"{path}: sequence {item.get('number', '?')} is missing or has a bad field ({exc!r})"
            ) from None
    if not sequences:
        raise BroadcastSyncError(f"{path} has no POV highlight sequences")
    return LoadedReport(
        path=path,
        demo_name=PureWindowsPath(str(data["demo"])).name,
        map_name=str(data.get("map", "")),
        tickrate=float(data.get("tickrate", 0) or 0),
        rounds=int(data.get("rounds", 0) or 0),
        sequences=sequences,
        raw=data,
    )


def same_demo_name(report: LoadedReport, demo_path: Path) -> bool:
    return report.demo_name.casefold() == demo_path.name.casefold()


def check_against_demo(report: LoadedReport, demo: DemoData, demo_path: Path) -> None:
    """Raise when the report cannot have been made from this demo."""
    name = report.path.name
    if not same_demo_name(report, demo_path):
        raise BroadcastSyncError(
            f"{name} was made from {report.demo_name!r}, not {demo_path.name!r}"
        )
    if report.map_name and report.map_name != demo.map_name:
        raise BroadcastSyncError(f"{name} is for map {report.map_name!r} but the demo is {demo.map_name!r}")
    if report.tickrate and abs(report.tickrate - float(demo.tickrate)) > 0.5:
        raise BroadcastSyncError(
            f"{name}: report tickrate {report.tickrate:g} does not match the demo's {float(demo.tickrate):g}"
        )
    if report.rounds and report.rounds != len(demo.rounds):
        raise BroadcastSyncError(
            f"{name}: report has {report.rounds} rounds but the demo has {len(demo.rounds)}; "
            "it was probably made from a different demo file"
        )
    for seq in report.sequences:
        where = f"{name} sequence {seq.number}"
        if not 1 <= seq.round <= len(demo.rounds):
            raise BroadcastSyncError(f"{where}: round {seq.round} does not exist in the demo ({len(demo.rounds)} rounds)")
        if not 0 <= seq.start_tick < seq.end_tick:
            raise BroadcastSyncError(f"{where}: bad tick range {seq.start_tick}-{seq.end_tick}")
        if demo.last_tick and seq.end_tick > demo.last_tick:
            raise BroadcastSyncError(
                f"{where}: ends at tick {seq.end_tick}, after the demo's last tick {demo.last_tick}"
            )
        stray = [t for t in seq.kill_ticks if not seq.start_tick - TICK_SLACK <= t <= seq.end_tick + TICK_SLACK]
        if stray:
            raise BroadcastSyncError(f"{where}: kill tick {stray[0]} is outside the clip's ticks {seq.start_tick}-{seq.end_tick}")
