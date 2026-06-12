"""Port of CSDM's JSONActionsFileGenerator (CS2 flavor).

Writes <demo>.json: a JSON array of {"actions": [{"tick", "cmd"}]} groups, one
per sequence. The CS2 server plugin reads it and executes each cmd when demo
playback reaches its tick. `pause_playback` and `go_to_next_sequence` are
pseudo-commands interpreted by the plugin itself.
"""

from __future__ import annotations

import json
from pathlib import Path

MIN_SAFE_TICK = 96  # ticks right after demo_gototick 0 may be skipped (CSDM #1343)


def _valid_tick(tick: int) -> int:
    return max(MIN_SAFE_TICK, tick)


class ActionsFile:
    def __init__(self, demo_path: Path) -> None:
        self.path = Path(str(demo_path) + ".json")
        self._sequences: list[dict] = []
        self._current: list[dict] = []

    def add_exec(self, tick: int, cmd: str) -> "ActionsFile":
        self._current.append({"tick": _valid_tick(tick), "cmd": cmd})
        return self

    def add_go_to_tick(self, at_tick: int, to_tick: int) -> "ActionsFile":
        return self.add_exec(at_tick, f"demo_gototick {_valid_tick(to_tick)}")

    def add_spec_player(self, tick: int, slot: int) -> "ActionsFile":
        # spec_mode must be set before spec_player, otherwise the camera can
        # stay stuck in free mode (see CSDM JSONActionsFileGenerator).
        self.add_exec(tick, "spec_mode 1")
        self.add_exec(tick, f"spec_player {slot}")
        return self

    def add_pause_playback(self, tick: int) -> "ActionsFile":
        return self.add_exec(tick, "pause_playback")

    def add_go_to_next_sequence(self, tick: int) -> "ActionsFile":
        self.add_exec(tick, "go_to_next_sequence")
        self._sequences.append({"actions": self._current})
        self._current = []
        return self

    def write(self) -> Path:
        sequences = list(self._sequences)
        if self._current:
            sequences.append({"actions": self._current})
        self.path.write_text(json.dumps(sequences, indent=2), encoding="utf-8")
        return self.path

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)
