"""Install/uninstall the CS2 server plugin (port of CSDM's cs2-server-plugin.ts).

The plugin (a server.dll shim) executes console commands at exact demo ticks,
driven by a <demo>.json actions file. The engine loads it via a temporary
gameinfo.gi search-path entry; the plugin restores gameinfo.gi itself on load,
and uninstall() restores it again as a safety net.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from highlights.recording.steam import get_gameinfo_path

GAMEINFO_ORIGINAL = "Game\tcsgo"
GAMEINFO_PATCHED = "Game\tcsgo/csdm\n\t\t\tGame\tcsgo"


def _plugin_folder(cs2_folder: Path) -> Path:
    return cs2_folder / "game" / "csgo" / "csdm"


def _backup_path(gameinfo: Path) -> Path:
    return gameinfo.with_name(gameinfo.name + ".backup")


def _log_file(cs2_folder: Path) -> Path:
    return cs2_folder / "game" / "bin" / "win64" / "csdm.log"


def install_plugin(cs2_folder: Path, plugin_dll: Path) -> None:
    bin_folder = _plugin_folder(cs2_folder) / "bin"
    bin_folder.mkdir(parents=True, exist_ok=True)
    _log_file(cs2_folder).unlink(missing_ok=True)
    shutil.copyfile(plugin_dll, bin_folder / "server.dll")

    gameinfo = get_gameinfo_path(cs2_folder)
    content = gameinfo.read_text(encoding="utf-8")
    if "Game\tcsgo/csdm" not in content:
        shutil.copyfile(gameinfo, _backup_path(gameinfo))
        gameinfo.write_text(content.replace(GAMEINFO_ORIGINAL, GAMEINFO_PATCHED, 1), encoding="utf-8")


def uninstall_plugin(cs2_folder: Path) -> None:
    folder = _plugin_folder(cs2_folder)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)

    gameinfo = get_gameinfo_path(cs2_folder)
    backup = _backup_path(gameinfo)
    if backup.is_file():
        shutil.copyfile(backup, gameinfo)
        backup.unlink()
