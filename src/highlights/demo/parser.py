"""Parse a CS2 demo with demoparser2 into plain model objects.

Everything downstream (analysis, recording) works off DemoData so the
demoparser2 API surface is isolated here.
"""

from __future__ import annotations

import gzip
import math
import shutil
import tempfile
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from demoparser2 import DemoParser

from highlights.models import Kill, Round

DEFAULT_TICKRATE = 64.0

# Side strings as demoparser2 reports them.
_SIDE_BY_TEAM_NUM = {2: "T", 3: "CT"}


@dataclass(frozen=True)
class Death:
    """Unfiltered death record, used for alive-tracking (clutch detection)."""

    tick: int
    round_index: int
    victim_steamid: str


@dataclass
class DemoData:
    path: Path
    map_name: str
    tickrate: float
    kills: list[Kill]  # filtered: no warmup, teamkills, suicides
    deaths: list[Death]  # unfiltered: every player death after match start
    rounds: list[Round]
    player_names: dict[str, str]  # steamid -> name
    slots: dict[str, int]  # steamid -> spec_player slot (may be empty, see get_slots)
    rosters: dict[int, dict[str, str]] = field(default_factory=dict)  # round -> {steamid: side}
    match_start_tick: int = 0
    last_tick: int = 0

    def round_at(self, index: int) -> Round | None:
        if 0 <= index < len(self.rounds):
            return self.rounds[index]
        return None


def ensure_dem_file(path: Path, work_dir: Path) -> Path:
    """Demos often arrive compressed (.gz from FACEIT, .zst from Valve);
    transparently decompress to the work dir."""
    suffix = path.suffix.lower()
    if suffix not in (".gz", ".zst"):
        return path
    target = work_dir / path.name[: -len(suffix)]
    if not target.suffix:
        target = target.with_suffix(".dem")
    target.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".gz":
        with gzip.open(path, "rb") as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    else:
        import zstandard

        with open(path, "rb") as src, open(target, "wb") as dst:
            zstandard.ZstdDecompressor().copy_stream(src, dst)
    return target


def _normalize_side(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _SIDE_BY_TEAM_NUM.get(int(value), "")
    text = str(value or "").upper()
    if text.startswith("T"):  # TERRORIST / T
        return "T"
    if text == "CT":
        return "CT"
    return ""


def _normalize_winner(value: object) -> str:
    return _normalize_side(value)


def _safe_event(parser: DemoParser, name: str, **kwargs) -> pd.DataFrame:
    try:
        df = parser.parse_event(name, **kwargs)
    except Exception:
        return pd.DataFrame()
    if df is None:
        return pd.DataFrame()
    return df


def _col(row: pd.Series, name: str, default=None):
    value = row.get(name, default)
    if pd.isna(value):
        return default
    return value


def _compute_tickrate(parser: DemoParser, sample_ticks: list[int]) -> float:
    """tickrate = Δtick / Δgame_time between two distant ticks; demo header has no rate."""
    if len(sample_ticks) < 2:
        return DEFAULT_TICKRATE
    t1, t2 = min(sample_ticks), max(sample_ticks)
    if t2 - t1 < 256:
        return DEFAULT_TICKRATE
    try:
        df = parser.parse_ticks(["game_time"], ticks=[t1, t2])
    except Exception:
        return DEFAULT_TICKRATE
    if df.empty:
        return DEFAULT_TICKRATE
    grouped = df.groupby("tick")["game_time"].first()
    if len(grouped) < 2:
        return DEFAULT_TICKRATE
    dt = grouped.index.max() - grouped.index.min()
    dgt = grouped.loc[grouped.index.max()] - grouped.loc[grouped.index.min()]
    if dgt <= 0:
        return DEFAULT_TICKRATE
    rate = dt / dgt
    if not math.isfinite(rate) or rate < 16 or rate > 256:
        return DEFAULT_TICKRATE
    return float(round(rate))


def _build_rounds(parser: DemoParser, match_start_tick: int) -> list[Round]:
    starts = _safe_event(parser, "round_start")
    ends = _safe_event(parser, "round_end")
    freeze_ends = _safe_event(parser, "round_freeze_end")

    start_ticks = sorted(int(t) for t in starts.get("tick", pd.Series(dtype=int)))
    freeze_ticks = sorted(int(t) for t in freeze_ends.get("tick", pd.Series(dtype=int)))

    rounds: list[Round] = []
    if ends.empty:
        return rounds
    ends = ends.sort_values("tick")
    for _, row in ends.iterrows():
        end_tick = int(row["tick"])
        if end_tick < match_start_tick:
            continue
        prior_starts = [t for t in start_ticks if t <= end_tick]
        start_tick = prior_starts[-1] if prior_starts else match_start_tick
        freeze_in_round = [t for t in freeze_ticks if start_tick <= t <= end_tick]
        freeze_end_tick = freeze_in_round[-1] if freeze_in_round else start_tick
        rounds.append(
            Round(
                index=len(rounds),
                start_tick=start_tick,
                freeze_end_tick=freeze_end_tick,
                end_tick=end_tick,
                winner=_normalize_winner(row.get("winner")),
            )
        )
    return rounds


def get_slots(parser: DemoParser, sample_tick: int) -> dict[str, int]:
    """steamid -> spec_player slot. CSDM uses slot = demo userid + 1 for CS2.

    Empirically unverified prop availability across demo types, so this is
    best-effort: returns {} when the prop is missing and the recorder warns.
    """
    try:
        df = parser.parse_ticks(["user_id"], ticks=[sample_tick])
    except Exception:
        return {}
    if df.empty or "user_id" not in df.columns:
        return {}
    slots: dict[str, int] = {}
    for _, row in df.iterrows():
        steamid = str(row.get("steamid", ""))
        user_id = row.get("user_id")
        if steamid and pd.notna(user_id):
            slots[steamid] = int(user_id) + 1
    return slots


def parse_demo(path: Path) -> DemoData:
    parser = DemoParser(str(path))

    try:
        header = parser.parse_header()
    except Exception:
        header = {}
    map_name = str(header.get("map_name", "unknown"))

    new_match = _safe_event(parser, "begin_new_match")
    match_start_tick = 0
    if not new_match.empty and "tick" in new_match.columns:
        match_start_tick = int(new_match["tick"].max())

    deaths_df = _safe_event(
        parser,
        "player_death",
        player=["team_name"],
        other=["is_warmup_period"],
    )
    if deaths_df.empty:
        raise ValueError("No player_death events found — is this a valid CS2 demo?")
    deaths_df = deaths_df.sort_values("tick")

    rounds = _build_rounds(parser, match_start_tick)
    if not rounds:
        raise ValueError("No round_end events found — demo looks incomplete")
    round_start_ticks = [r.start_tick for r in rounds]

    def round_index_for_tick(tick: int) -> int:
        idx = bisect_right(round_start_ticks, tick) - 1
        return max(0, idx)

    kills: list[Kill] = []
    deaths: list[Death] = []
    for _, row in deaths_df.iterrows():
        tick = int(row["tick"])
        if tick < match_start_tick:
            continue
        if bool(_col(row, "is_warmup_period", False)):
            continue
        victim_steamid = str(_col(row, "user_steamid", "") or "")
        round_index = round_index_for_tick(tick)
        if victim_steamid:
            deaths.append(Death(tick=tick, round_index=round_index, victim_steamid=victim_steamid))

        attacker_steamid = str(_col(row, "attacker_steamid", "") or "")
        if not attacker_steamid or attacker_steamid == victim_steamid:
            continue  # suicide / world death
        attacker_side = _normalize_side(_col(row, "attacker_team_name", ""))
        victim_side = _normalize_side(_col(row, "user_team_name", ""))
        if attacker_side and victim_side and attacker_side == victim_side:
            continue  # teamkill
        kills.append(
            Kill(
                tick=tick,
                round_index=round_index,
                attacker_steamid=attacker_steamid,
                attacker_name=str(_col(row, "attacker_name", "") or ""),
                attacker_side=attacker_side,
                victim_steamid=victim_steamid,
                victim_name=str(_col(row, "user_name", "") or ""),
                weapon=str(_col(row, "weapon", "") or ""),
                headshot=bool(_col(row, "headshot", False)),
                noscope=bool(_col(row, "noscope", False)),
                penetrated=int(_col(row, "penetrated", 0) or 0),
                thrusmoke=bool(_col(row, "thrusmoke", False)),
                attackerblind=bool(_col(row, "attackerblind", False)),
            )
        )

    player_names: dict[str, str] = {}
    try:
        info = parser.parse_player_info()
        for _, row in info.iterrows():
            steamid = str(row.get("steamid", ""))
            if steamid:
                player_names[steamid] = str(row.get("name", ""))
    except Exception:
        pass
    for kill in kills:
        player_names.setdefault(kill.attacker_steamid, kill.attacker_name)
        player_names.setdefault(kill.victim_steamid, kill.victim_name)

    death_ticks = [d.tick for d in deaths]
    tickrate = _compute_tickrate(parser, death_ticks)
    last_tick = max(
        rounds[-1].end_tick,
        death_ticks[-1] if death_ticks else 0,
    ) + int(10 * tickrate)

    rosters = _build_rosters(parser, rounds)
    slots = get_slots(parser, rounds[0].freeze_end_tick + 16)

    return DemoData(
        path=path,
        map_name=map_name,
        tickrate=tickrate,
        kills=kills,
        deaths=deaths,
        rounds=rounds,
        player_names=player_names,
        slots=slots,
        rosters=rosters,
        match_start_tick=match_start_tick,
        last_tick=last_tick,
    )


def _build_rosters(parser: DemoParser, rounds: list[Round]) -> dict[int, dict[str, str]]:
    """Alive players per side shortly after each round's freeze end."""
    sample_by_tick: dict[int, int] = {}
    for r in rounds:
        sample_by_tick[r.freeze_end_tick + 16] = r.index
    try:
        df = parser.parse_ticks(["is_alive", "team_num"], ticks=list(sample_by_tick.keys()))
    except Exception:
        return {}
    rosters: dict[int, dict[str, str]] = {r.index: {} for r in rounds}
    if df.empty:
        return rosters
    for _, row in df.iterrows():
        tick = int(row["tick"])
        round_index = sample_by_tick.get(tick)
        if round_index is None:
            continue
        if not bool(row.get("is_alive", False)):
            continue
        side = _normalize_side(row.get("team_num"))
        if side not in ("CT", "T"):
            continue
        steamid = str(row.get("steamid", ""))
        if steamid:
            rosters[round_index][steamid] = side
    return rosters
