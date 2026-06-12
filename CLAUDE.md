# CLAUDE.md

CS2 demo → highlights MP4 generator. Pipeline: parse (demoparser2) → detect
moments → record real footage by driving CS2+HLAE → FFmpeg post-process/concat.
Windows-only at the recording stage; analysis runs anywhere.

## Commands

```powershell
.venv\Scripts\python -m pytest tests -q                  # unit tests (synthetic fixtures, no demo needed)
.venv\Scripts\highlights doctor                          # environment diagnostic (run before touching recording)
.venv\Scripts\highlights <demo> --json-only -o out.mp4   # analysis smoke test, writes out.json + rich table
.venv\Scripts\highlights record-one <demo> --start-tick A --end-tick B --steamid S   # hidden: single-clip recording PoC
.venv\Scripts\highlights update-tools                    # re-download latest HLAE/FFmpeg after a CS2 update
```

CLI: click with a custom `DefaultRunGroup` — `highlights match.dem` resolves to
the `run` subcommand. Entry point `highlights.cli:main`.

## Architecture (src/highlights/)

- `demo/parser.py` — the ONLY file touching the demoparser2 API; produces
  `DemoData` (kills, rounds, rosters, slots, tickrate). Tickrate is computed
  from Δtick/Δgame_time (not in the header), fallback 64.
- `analysis/` — `moments.py` (cluster kills per player/round), `clutch.py`
  (replay deaths vs rosters → 1vX), `scoring.py` (all knobs in `WEIGHTS`),
  `selection.py` (top-N, clip tick bounds, overlap merge + camera plan).
- `recording/` — port of CS Demo Manager's (MIT) recording flow:
  `steam.py` (registry+vdf autodetect), `toolchain.py` (HLAE/FFmpeg download),
  `plugin.py` (server.dll install + gameinfo.gi patch), `actions_file.py`
  (`<demo>.json` the plugin executes), `sequences.py` (per-clip command plan),
  `hlae.py` (launch/inject/wait/crash-detect), `recorder.py` (orchestrator).
- `video/` — wav→aac mux, optional drawtext overlay, concat demuxer
  (stream-copy default) or xfade chain.

## Recording invariants — do not "simplify" these

Ported from CSDM source (verified June 2026; local reference copies in
`.research/csdm/`, gitignored). Each rule guards a real regression:

- Ticks in the actions file are clamped to ≥ 96 (game skips early ticks after
  `demo_gototick 0`, CSDM #1343).
- `demo_gototick` must run on an EARLIER tick than any `spec_player`
  (same-tick combo gets spec_player ignored since an Oct 2025 CS2 update,
  CSDM #1238), and `spec_mode 1` must precede `spec_player`.
- Camera slot = demo `user_id` + 1 (CSDM convention). **Not yet validated
  in-game** — first thing to verify when recording becomes possible.
- `pause_playback` goes at `start_tick - 4` (engine may skip ticks between
  pause request and actual pause).
- FFmpeg preset string: `{QUOTE}` is HLAE's quote placeholder; output folder
  uses forward slashes; the literal `\\` before `video.mp4` is unescaped by
  HLAE. Don't normalize any of it.
- `-insecure` in game args is mandatory (the plugin hard-aborts without it).
- Plugin lifecycle: install copies server.dll to `<cs2>/game/csgo/csdm/bin/`
  and patches `gameinfo.gi` (backup → `gameinfo.gi.backup`); uninstall MUST
  run in `finally` — leaving gameinfo patched breaks the user's game on
  VAC servers.
- Expected outputs per sequence: `<work>/N-sequence/video.mp4` +
  `take*/audio.wav` (use the last take folder).

## Environment gotchas (this machine)

- `python` is **Microsoft Store Python**: MSIX virtualization makes its
  AppData/%TEMP% writes invisible to external processes (HLAE, CS2, FFmpeg).
  That's why app data lives in `~/.cs2-highlights` — never move it back to
  AppData, never default work dirs to %TEMP%.
- **HLAE vs CS2 build compatibility is the recurring blocker**: a CS2 update
  breaks injection (game dies ~2 s in with an `*_accessviolation.mdmp` next to
  cs2.exe — crashes go to Valve breakpad, NOT Windows event log). `doctor` has
  an "HLAE vs CS2 build" date check; fix = `update-tools` once advancedfx
  releases. As of 2026-06-12 recording is blocked exactly this way
  (CS2 1.41.6.5 of Jun 10 vs HLAE v2.190.2 of May 27); analysis is fully
  verified, end-to-end recording is not.
- Refreshing `vendor/cs2-plugin/server.dll`: extract
  `resources/static/cs2/server*.dll` from the latest CSDM Windows installer.
  py7zr cannot decode it (BCJ2 filter) — carve the 7z payload at the
  signature offset and use standalone `7zr.exe`. Script kept locally in
  `.research/extract_plugin.py`.
- Local test demo: `C:\Users\OS\Downloads\inferno.dem` (de_inferno, 21 rounds,
  tickrate 64). Known-good PoC clip once HLAE works again:
  `record-one ... --start-tick 45771 --end-tick 47404 --steamid 76561198152383695`
  (Auttnic 4K, round 5).

## Verifying changes

1. `pytest` — detector/actions-file logic (fixtures built in `tests/conftest.py`).
2. `--json-only` on the local demo — the known highlights must appear
   (Auttnic 4K R5 top-scored or near; clutches in R6/R11/R13/R20).
3. Recording changes: `record-one` PoC first, watch the clip (correct POV,
   sound present), then a full run. Afterwards assert the CS2 folder is
   pristine: no `csdm/` dir, no `gameinfo.gi.backup`, no `<demo>.json` left.
