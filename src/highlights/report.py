"""highlights.json report and the rich table shown after analysis."""

from __future__ import annotations

import json
from pathlib import Path

from rich.table import Table

from highlights.demo.parser import DemoData
from highlights.models import Sequence
from highlights.util.log import console


def write_report(path: Path, demo: DemoData, sequences: list[Sequence]) -> None:
    data = {
        "demo": str(demo.path),
        "map": demo.map_name,
        "tickrate": demo.tickrate,
        "rounds": len(demo.rounds),
        "sequences": [
            {
                "number": s.number,
                "label": s.label,
                "score": s.score,
                "round": s.round_index + 1,
                "start_tick": s.start_tick,
                "end_tick": s.end_tick,
                "duration_seconds": round(s.duration_seconds(demo.tickrate), 1),
                "cameras": [
                    {"tick": c.tick, "steamid": c.steamid, "name": demo.player_names.get(c.steamid, "")}
                    for c in s.cameras
                ],
                "moments": [
                    {
                        "player": m.name,
                        "steamid": m.steamid,
                        "kills": m.kill_count,
                        "description": m.describe(),
                        "score": m.score,
                        "clutch": (
                            {
                                "opponents": m.clutch.opponents,
                                "won": m.clutch.won,
                                "kills": m.clutch.kills,
                            }
                            if m.clutch
                            else None
                        ),
                    }
                    for m in s.moments
                ],
            }
            for s in sequences
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def print_sequences_table(demo: DemoData, sequences: list[Sequence]) -> None:
    table = Table(title=f"Highlights — {demo.map_name} ({len(demo.rounds)} rounds)")
    table.add_column("#", justify="right")
    table.add_column("Round", justify="right")
    table.add_column("Highlight")
    table.add_column("Score", justify="right")
    table.add_column("Length", justify="right")
    table.add_column("Ticks", justify="right")
    for s in sequences:
        table.add_row(
            str(s.number),
            str(s.round_index + 1),
            s.label,
            f"{s.score:.1f}",
            f"{s.duration_seconds(demo.tickrate):.0f}s",
            f"{s.start_tick}-{s.end_tick}",
        )
    console.print(table)
