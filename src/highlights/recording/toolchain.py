"""Locate or auto-install HLAE, FFmpeg and the CS2 server plugin DLL."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import requests

from highlights.config import Config, app_data_dir
from highlights.util.log import info

HLAE_RELEASES_API = "https://api.github.com/repos/advancedfx/advancedfx/releases"
FFMPEG_DOWNLOAD_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
)

# Vendored CSDM plugin (MIT, https://github.com/akiver/cs-demo-manager) and the
# fallback location of an installed CS Demo Manager.
VENDOR_PLUGIN_PATH = Path(__file__).resolve().parents[3] / "vendor" / "cs2-plugin" / "server.dll"
CSDM_INSTALL_PLUGIN_CANDIDATES = (
    Path.home() / "AppData" / "Local" / "Programs" / "CS Demo Manager" / "resources" / "static" / "cs2" / "server.dll",
    Path.home() / "AppData" / "Local" / "Programs" / "cs-demo-manager" / "resources" / "static" / "cs2" / "server.dll",
)


def tools_dir() -> Path:
    return app_data_dir() / "tools"


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=600) as response:
        response.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1 << 20):
                fh.write(chunk)


def find_hlae(config: Config) -> Path | None:
    """Path to HLAE.exe, or None if not installed."""
    if config.paths.hlae_dir != "auto":
        exe = Path(config.paths.hlae_dir) / "HLAE.exe"
        return exe if exe.is_file() else None
    exe = tools_dir() / "hlae" / "HLAE.exe"
    return exe if exe.is_file() else None


def install_hlae() -> Path:
    info("Downloading HLAE from GitHub releases...")
    releases = requests.get(HLAE_RELEASES_API, timeout=60).json()
    asset_url = None
    release_info: dict = {}
    for release in releases:
        if release.get("prerelease") or release.get("draft"):
            continue
        for asset in release.get("assets", []):
            name = asset.get("name", "")
            if name.lower().endswith(".zip") and "source" not in name.lower():
                asset_url = asset["browser_download_url"]
                release_info = {
                    "tag": release.get("tag_name", ""),
                    "published_at": release.get("published_at", ""),
                }
                break
        if asset_url:
            break
    if not asset_url:
        raise RuntimeError("Could not find an HLAE release zip on GitHub")

    dest_dir = tools_dir() / "hlae"
    zip_path = tools_dir() / "hlae.zip"
    _download(asset_url, zip_path)
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    zip_path.unlink()

    exe = dest_dir / "HLAE.exe"
    if not exe.is_file():
        # The zip may contain a single top-level folder.
        candidates = list(dest_dir.glob("**/HLAE.exe"))
        if not candidates:
            raise RuntimeError(f"HLAE.exe not found after extracting to {dest_dir}")
        exe = candidates[0]
    (exe.parent / "install-info.json").write_text(json.dumps(release_info), encoding="utf-8")
    info(f"HLAE installed: {exe} ({release_info.get('tag', '?')})")
    return exe


def read_hlae_install_info(hlae_exe: Path) -> dict:
    info_path = hlae_exe.parent / "install-info.json"
    if not info_path.is_file():
        return {}
    try:
        return json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def find_ffmpeg(config: Config) -> Path | None:
    if config.paths.ffmpeg_exe != "auto":
        exe = Path(config.paths.ffmpeg_exe)
        return exe if exe.is_file() else None
    exe = tools_dir() / "ffmpeg" / "bin" / "ffmpeg.exe"
    if exe.is_file():
        return exe
    on_path = shutil.which("ffmpeg")
    return Path(on_path) if on_path else None


def install_ffmpeg() -> Path:
    info("Downloading FFmpeg (BtbN win64-gpl build)...")
    dest_dir = tools_dir() / "ffmpeg"
    zip_path = tools_dir() / "ffmpeg.zip"
    _download(FFMPEG_DOWNLOAD_URL, zip_path)
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    zip_path.unlink()

    candidates = list(dest_dir.glob("**/bin/ffmpeg.exe"))
    if not candidates:
        raise RuntimeError(f"ffmpeg.exe not found after extracting to {dest_dir}")
    exe = candidates[0]
    # Normalize layout to <tools>/ffmpeg/bin/ffmpeg.exe for find_ffmpeg.
    normalized = dest_dir / "bin" / "ffmpeg.exe"
    if exe != normalized:
        normalized.parent.mkdir(parents=True, exist_ok=True)
        for sibling in exe.parent.iterdir():
            shutil.move(str(sibling), str(normalized.parent / sibling.name))
        # Drop the now-empty release folder (bin/, doc/, etc.).
        release_root = exe.parent.parent
        if release_root != dest_dir:
            shutil.rmtree(release_root, ignore_errors=True)
    info(f"FFmpeg installed: {normalized}")
    return normalized


def find_plugin_dll(config: Config) -> Path | None:
    if config.paths.plugin_dll != "auto":
        dll = Path(config.paths.plugin_dll)
        return dll if dll.is_file() else None
    if VENDOR_PLUGIN_PATH.is_file():
        return VENDOR_PLUGIN_PATH
    for candidate in CSDM_INSTALL_PLUGIN_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def write_ffmpeg_ini(hlae_exe: Path, ffmpeg_exe: Path) -> None:
    """HLAE locates FFmpeg via <hlae>/ffmpeg/ffmpeg.ini (see CSDM registerFfmpegLocation)."""
    ini_dir = hlae_exe.parent / "ffmpeg"
    ini_dir.mkdir(parents=True, exist_ok=True)
    (ini_dir / "ffmpeg.ini").write_text(f"[Ffmpeg]\nPath={ffmpeg_exe}", encoding="utf-8")
