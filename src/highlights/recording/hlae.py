"""Launch CS2 through HLAE's custom loader (port of start-counter-strike-with-hlae.ts)."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from highlights.config import app_data_dir
from highlights.recording.sequences import to_unix_path
from highlights.util.log import info
from highlights.util.process import (
    find_processes,
    kill_processes,
    wait_for_process_exit,
    wait_for_process_start,
)

GAME_PROCESS = "cs2.exe"

CRASH_ADVICE = (
    "The game crashed during startup (minidump created). The most common cause is an "
    "HLAE incompatible with the current CS2 build: CS2 updates regularly break the "
    "injection until advancedfx ships a new HLAE release (usually within days). "
    "Run `highlights update-tools` to fetch the latest HLAE, or check "
    "https://github.com/advancedfx/advancedfx/releases"
)


class HlaeError(RuntimeError):
    pass


class GameError(RuntimeError):
    pass


def find_minidumps_since(cs2_exe: Path, since_timestamp: float) -> list[Path]:
    """CS2 writes <name>_accessviolation.mdmp next to cs2.exe on crash
    (crashes go to Valve's breakpad, not Windows WER, so this is the only trace)."""
    dumps = []
    for dump in cs2_exe.parent.glob("*.mdmp"):
        try:
            if dump.stat().st_mtime >= since_timestamp:
                dumps.append(dump)
        except OSError:
            continue
    return dumps


def build_hlae_args(
    hlae_exe: Path,
    cs2_exe: Path,
    demo_path: Path,
    width: int,
    height: int,
) -> list[str]:
    # Forward slashes in the demo path avoid backslash-before-quote escaping
    # issues inside the nested -cmdLine string.
    game_args = (
        f'-insecure -novid +playdemo "{to_unix_path(demo_path)}"'
        f" -width {width} -height {height} -sw"
    )
    return [
        str(hlae_exe),
        "-noGui",
        "-autoStart",
        "-noConfig",
        "-afxDisableSteamStorage",
        "-customLoader",
        "-hookDllPath",
        str(hlae_exe.parent / "x64" / "AfxHookSource2.dll"),
        "-programPath",
        str(cs2_exe),
        "-cmdLine",
        game_args,
    ]


def _hlae_error_window_exists() -> bool:
    result = subprocess.run(
        ["tasklist", "/fi", f"imagename eq {GAME_PROCESS}", "/v", "/nh"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return "Error - AfxHookSource" in (result.stdout or "")


def run_game_with_hlae(
    hlae_exe: Path,
    cs2_exe: Path,
    demo_path: Path,
    width: int,
    height: int,
    timeout_seconds: float,
) -> None:
    """Inject, play the demo, and block until cs2.exe exits (or timeout)."""
    if kill_processes(GAME_PROCESS):
        info("Killed a running CS2 instance, waiting for file locks to release...")
        time.sleep(2)

    # Sandbox the game's user config so we don't clobber the player's settings.
    cfg_sandbox = app_data_dir() / "cs2-cfg"
    cfg_sandbox.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, USRLOCALCSGO=str(cfg_sandbox))

    launch_time = time.time()
    args = build_hlae_args(hlae_exe, cs2_exe, demo_path, width, height)
    result = subprocess.run(args, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise HlaeError(
            f"HLAE exited with code {result.returncode}: {result.stderr or result.stdout}"
        )

    time.sleep(2)
    if _hlae_error_window_exists():
        raise HlaeError("HLAE injection failed (Error - AfxHookSource window detected)")

    def crash_suffix() -> str:
        dumps = find_minidumps_since(cs2_exe, launch_time)
        return f"\n{CRASH_ADVICE}\nMinidump: {dumps[-1]}" if dumps else ""

    game = wait_for_process_start(GAME_PROCESS, timeout=30)
    if game is None:
        raise GameError(f"CS2 did not start (or crashed immediately).{crash_suffix()}")

    info("CS2 is running, recording in progress (the game will close itself when done)...")
    if not wait_for_process_exit(game, timeout=timeout_seconds):
        kill_processes(GAME_PROCESS)
        raise GameError(
            f"Recording did not finish within {int(timeout_seconds)}s watchdog — killed CS2"
        )

    dumps = find_minidumps_since(cs2_exe, launch_time)
    if dumps:
        raise GameError(f"CS2 crashed during recording.{crash_suffix()}")
