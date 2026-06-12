"""Resolve a --player query (steamid64 or name) against the demo's players."""

from __future__ import annotations

from highlights.demo.parser import DemoData


class PlayerNotFound(ValueError):
    pass


def resolve_player(demo: DemoData, query: str) -> str:
    """Return the steamid64 for a steamid or (partial, case-insensitive) name."""
    query = query.strip()
    if query.isdigit() and len(query) == 17:
        if query in demo.player_names:
            return query
        raise PlayerNotFound(f"SteamID {query} not found in this demo")

    lowered = query.lower()
    exact = [sid for sid, name in demo.player_names.items() if name.lower() == lowered]
    if len(exact) == 1:
        return exact[0]
    partial = [sid for sid, name in demo.player_names.items() if lowered in name.lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(sorted(demo.player_names[sid] for sid in partial))
        raise PlayerNotFound(f"'{query}' matches several players: {names}")
    available = ", ".join(sorted(demo.player_names.values()))
    raise PlayerNotFound(f"Player '{query}' not found. Players in demo: {available}")
