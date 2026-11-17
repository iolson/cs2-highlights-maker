"""Demo tick -> VOD time, per segment of continuous live play.

Inside a segment VOD time advances 1:1 with game time, so an anchor maps any tick as
`vod = anchor.vod + (tick - anchor.tick) / tickrate` (nearest anchor wins). Anchors that
disagree beyond the drift tolerance are rejected instead of averaged or stretched:
the disagreement means a pause, replay or cut sits between them and the segment must
be split. Nothing is ever interpolated across segments.
"""

from __future__ import annotations

from bisect import bisect_right

from highlights.broadcast.models import Anchor, AnchorFile, BroadcastSyncError, Segment
from highlights.demo.parser import DemoData

DRIFT_TOLERANCE = 0.5  # seconds two anchors of one segment may disagree by
POST_ROUND_SECONDS = 5.0  # live broadcast kept after round_end (reaction, replay not yet started)


class Timeline:
    def __init__(self, demo: DemoData, anchors: AnchorFile, drift_tolerance: float = DRIFT_TOLERANCE):
        if not demo.rounds:
            raise BroadcastSyncError("The demo has no rounds, so no anchors can be placed")
        self.demo = demo
        self.tickrate = float(demo.tickrate)
        self.drift_tolerance = drift_tolerance
        self.segments = list(anchors.segments)
        self.ignored: list[tuple[str, Anchor]] = []  # (segment id, anchor) skipped because not verified
        self._round_starts = [r.start_tick for r in demo.rounds]
        self._rounds: dict[str, tuple[int, int]] = {}
        self._usable: dict[str, list[Anchor]] = {}
        self._validate()

    # --- geometry -------------------------------------------------------------------

    def round_of_tick(self, tick: float) -> int | None:
        """1-based round whose span contains the tick (None before the first round)."""
        index = bisect_right(self._round_starts, tick) - 1
        return index + 1 if index >= 0 else None

    def rounds_of(self, segment: Segment) -> tuple[int, int]:
        return self._rounds[segment.id]

    def tick_range(self, segment: Segment) -> tuple[int, int]:
        """First tick of the segment's first round to a little after its last round ends."""
        first, last = self.rounds_of(segment)
        rounds = self.demo.rounds
        end = rounds[last - 1].end_tick + round(POST_ROUND_SECONDS * self.tickrate)
        end = min(end, rounds[last].start_tick if last < len(rounds) else (self.demo.last_tick or end))
        return rounds[first - 1].start_tick, end

    def segment_for_round(self, round_number: int) -> Segment | None:
        for segment in self.segments:
            first, last = self.rounds_of(segment)
            if first <= round_number <= last:
                return segment
        return None

    # --- mapping ---------------------------------------------------------------------

    def usable_anchors(self, segment: Segment) -> list[Anchor]:
        return self._usable[segment.id]

    def offset(self, anchor: Anchor) -> float:
        """VOD seconds at demo tick 0, as this anchor sees it."""
        return anchor.vod_seconds - anchor.demo_tick / self.tickrate

    def nearest_anchor(self, segment: Segment, tick: float) -> Anchor | None:
        anchors = self._usable[segment.id]
        if not anchors:
            return None
        return min(anchors, key=lambda a: (abs(a.demo_tick - tick), a.demo_tick))

    def to_vod(self, segment: Segment, tick: float) -> float:
        anchor = self.nearest_anchor(segment, tick)
        if anchor is None:
            raise BroadcastSyncError(f"Segment {segment.id!r} has no verified anchor")
        return anchor.vod_seconds + (tick - anchor.demo_tick) / self.tickrate

    # --- validation ------------------------------------------------------------------

    def _validate(self) -> None:
        total = len(self.demo.rounds)
        seen: set[str] = set()
        for segment in self.segments:
            if segment.id in seen:
                raise BroadcastSyncError(f"Duplicate segment id {segment.id!r}")
            seen.add(segment.id)
            first = segment.round_start if segment.round_start is not None else 1
            last = segment.round_end if segment.round_end is not None else total
            if not 1 <= first <= last <= total:
                raise BroadcastSyncError(
                    f"Segment {segment.id!r}: rounds {first}-{last} are not valid for a demo with {total} rounds"
                )
            self._rounds[segment.id] = (first, last)

        ordered = sorted(self.segments, key=lambda s: self._rounds[s.id])
        for before, after in zip(ordered, ordered[1:]):
            if self._rounds[after.id][0] <= self._rounds[before.id][1]:
                raise BroadcastSyncError(
                    f"Segments {before.id!r} (rounds {self._rounds[before.id][0]}-{self._rounds[before.id][1]}) and "
                    f"{after.id!r} (rounds {self._rounds[after.id][0]}-{self._rounds[after.id][1]}) overlap; "
                    "each round may belong to one segment only"
                )

        for segment in self.segments:
            self._validate_anchors(segment)

    def _validate_anchors(self, segment: Segment) -> None:
        low, high = self.tick_range(segment)
        first, last = self._rounds[segment.id]
        usable: list[Anchor] = []
        for anchor in segment.anchors:
            if not anchor.verified:
                self.ignored.append((segment.id, anchor))
                continue
            if not low <= anchor.demo_tick <= high:
                raise BroadcastSyncError(
                    f"Segment {segment.id!r}: anchor at tick {anchor.demo_tick} lies outside the segment's "
                    f"rounds {first}-{last} (ticks {low}-{high})"
                )
            usable.append(anchor)

        for before, after in zip(usable, usable[1:]):
            if after.demo_tick == before.demo_tick:
                raise BroadcastSyncError(f"Segment {segment.id!r}: two anchors at tick {after.demo_tick}")
            if after.vod_seconds <= before.vod_seconds:
                raise BroadcastSyncError(
                    f"Segment {segment.id!r}: the VOD goes backwards between tick {before.demo_tick} "
                    f"({before.vod_seconds:.3f}s) and tick {after.demo_tick} ({after.vod_seconds:.3f}s)"
                )
        for i, a in enumerate(usable):
            for b in usable[i + 1 :]:
                game = (b.demo_tick - a.demo_tick) / self.tickrate
                vod = b.vod_seconds - a.vod_seconds
                if abs(vod - game) > self.drift_tolerance:
                    raise BroadcastSyncError(
                        f"Segment {segment.id!r}: anchors at ticks {a.demo_tick} and {b.demo_tick} disagree: "
                        f"the VOD advanced {vod:.2f}s but the demo only {game:.2f}s "
                        f"(tolerance {self.drift_tolerance}s). A pause, replay or broadcast cut probably sits "
                        "between them; split the segment there instead of stretching time."
                    )
        self._usable[segment.id] = usable
