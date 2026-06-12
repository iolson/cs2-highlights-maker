"""Locate Steam and the CS2 installation (port of CSDM's get-*-path helpers)."""

from __future__ import annotations

import winreg
from pathlib import Path

import vdf

from highlights.util.process import is_process_running

CS_APP_ID = "730"
CS_FOLDER_NAME = "Counter-Strike Global Offensive"


def get_steam_folder() -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _ = winreg.QueryValueEx(key, "SteamPath")
    except OSError:
        return None
    if not value:
        return None
    return Path(str(value))


def get_cs2_folder() -> Path | None:
    """The "Counter-Strike Global Offensive" folder from libraryfolders.vdf."""
    steam = get_steam_folder()
    if steam is None:
        return None
    vdf_path = steam / "steamapps" / "libraryfolders.vdf"
    if not vdf_path.is_file():
        return None
    try:
        data = vdf.loads(vdf_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    folders = data.get("libraryfolders", {})
    for entry in folders.values():
        if not isinstance(entry, dict):
            continue
        apps = entry.get("apps") or {}
        if CS_APP_ID not in apps:
            continue
        folder = Path(entry.get("path", "")) / "steamapps" / "common" / CS_FOLDER_NAME
        if folder.is_dir():
            return folder
    return None


def get_cs2_exe(cs2_folder: Path) -> Path:
    return cs2_folder / "game" / "bin" / "win64" / "cs2.exe"


def get_gameinfo_path(cs2_folder: Path) -> Path:
    return cs2_folder / "game" / "csgo" / "gameinfo.gi"


def cs2_folder_from_exe(cs2_exe: Path) -> Path:
    # <cs2>\game\bin\win64\cs2.exe -> <cs2>
    return cs2_exe.parent.parent.parent.parent


def read_steam_inf(cs2_folder: Path) -> dict[str, str]:
    """Key/value pairs from game/csgo/steam.inf (PatchVersion, VersionDate, ...)."""
    inf_path = cs2_folder / "game" / "csgo" / "steam.inf"
    result: dict[str, str] = {}
    if not inf_path.is_file():
        return result
    for line in inf_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def is_steam_running() -> bool:
    return is_process_running("steam.exe")
