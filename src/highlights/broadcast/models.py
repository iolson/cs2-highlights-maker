"""Plain data types for anchors, segments and the planned audio windows."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class BroadcastSyncError(Exception):
    """Bad input (anchors, reports, media) with a message meant for the user."""


@dataclass(frozen=True)
class Anchor:
    """One demo tick pinned to one VOD time, e.g. 'the round-5 freeze ended here'."""

    demo_tick: int
    vod_seconds: float
    event: str = "manual"
    source: str = "manual"
    verified: bool = True


@dataclass(frozen=True)
class Segment:
    """A stretch of continuous live play: VOD time advances 1:1 with game time.

    round_start/round_end are 1-based and inclusive; None = the demo's first/last round.
    """

    id: str
    kind: str
    round_start: int | None
    round_end: int | None
    anchors: tuple[Anchor, ...]


@dataclass
class AnchorFile:
    path: Path
    match_id: str | None
    demo: str
    vod: str | None
    segments: list[Segment] = field(default_factory=list)


@dataclass(frozen=True)
class Window:
    """The VOD range exported for one clip, after clamping."""

    tick_start: float
    tick_end: float
    vod_start: float
    vod_end: float
    clamps: tuple[str, ...] = ()

    @property
    def duration(self) -> float:
        return self.vod_end - self.vod_start


@dataclass
class ClipPlan:
    """Planned broadcast audio for one highlight sequence."""

    report: Path
    sequence_number: int
    round: int
    start_tick: int
    end_tick: int
    status: str  # matched / needs_review / unmatched
    basis: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    segment_id: str | None = None
    anchors_used: list[dict] = field(default_factory=list)
    window: Window | None = None
    clip_start_vod: float | None = None
    events: list[dict] = field(default_factory=list)
    sequence: dict = field(default_factory=dict)
    stem: str = ""  # output file stem, set by the planner
