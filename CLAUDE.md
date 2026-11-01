# CLAUDE.md

CS2 demo → highlights MP4 generator. Pipeline: parse (demoparser2) → detect
moments → record real footage by driving CS2+HLAE → FFmpeg post-process/concat.
Windows-only at the recording stage; analysis runs anywhere.

## Commands

```powershell
.venv\Scripts\python -m pytest tests -q                  # unit tests (synthetic fixtures, no demo needed)
.venv\Scripts\highlights doctor                          # environment diagnostic (run before touching recording)
.venv\Scripts\highlights <demo> --json-only -o out.mp4   # analysis smoke test, writes out.json + rich table
.venv\Scripts\highlights <demo> --json-only --cinematic   # preview cam-path matches (needs files in campaths/)
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
  `hlae.py` (launch/inject/wait/crash-detect), `recorder.py` (orchestrator),
  `campaths.py` (`--cinematic`: load user cam paths, match them to plays).
- `spatial.py` — per-clip world-coordinate metadata (engagements, zones,
  keyframes, POV trajectories) sampled from the demo; feeds the report and
  cam-path matching. The only other file besides `parser.py` using demoparser2.
- `report.py` — the JSON report (`schema_version` 3). `write_report` runs
  BEFORE recording (timeline events with `seconds_from_clip_start` and nominal
  frame estimates); `finalize_report` runs AFTER concat and adds
  `video_start_seconds`, per-kill `video_time_seconds` and a flat
  `beat_targets` list (POV kills only) from ffprobe clip durations, crossfade
  overlap and `--timing-offset`. Times are nominal until calibrated against a
  real clip (`editing.timing_status`).
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
- `record()` deletes each `<work>/N-sequence/` before recording: the work dir
  is per demo and the ffmpeg preset has no `-y`, so a stale `video.mp4` (from
  `--keep-clips` on an earlier run) would block the new recording and be
  returned as its result.
- Expected outputs per sequence: `<work>/N-sequence/video.mp4` +
  `take*/audio.wav` (use the last take folder).

## Capture defaults (sequences.py, LANChamp setup)

Set per clip in `build_actions_file`; they deviate from stock CSDM on purpose.

- **Audio:** game sound stays, CS2 music is muted (`AUDIO_COMMANDS`:
  `snd_musicvolume`, menu/round/MVP/objective/deathcam volumes at 0), and
  `snd_mute_losefocus 0` keeps sound when CS2 isn't the foreground window.
- **Crosshair:** one fixed green crosshair for every observed player
  (`CROSSHAIR_COMMANDS`, tuned for 1080p; the cvars follow CS2's September 2026
  rename — recheck them if a CS2 update changes crosshair behavior).
  `cl_show_observer_crosshair 0` is set alongside it.
- **Kill feed:** only the active POV player's kills appear
  (`mirv_deathmsg filter add attackerMatch=!x<steamid> block=1 lastRule=1`,
  plus `localPlayer x<steamid>` so they are highlighted). The filter is
  re-applied at setup and on every camera switch, together with
  `mirv_deathmsg clear`, so the feed never carries the previous POV's kills.
- **Kill-feed lifetime:** `max(60, clip seconds + 10)` with `lifetimeMod 1.0`,
  so POV kills persist to the end of the clip instead of fading after 5 s.
  `recording.death_notices_duration` is therefore no longer used here.
- Every sequence must have at least one camera (raises otherwise): the first
  camera's player seeds the kill-feed filter.

## Cinematic cam paths (`--cinematic`)

- Cam paths are third-party/purchased and MUST NEVER be committed or copied
  into the repo (docs, tests, fixtures). They live in the gitignored
  `campaths/<Map>/` folder (`campaths/*`, `*.dz`, `*.campath` are ignored;
  only `campaths/README.md` is tracked) or wherever `paths.campaths_dir`
  points. Tests generate their own tiny campath XML.
- Flow: `cli.run` builds spatial metadata once → `add_cinematic_sequences`
  appends, after each POV sequence, `--cinematic-takes N` copies over the same
  ticks with `Sequence.campath` set (renumbered 1..N, `cinematic_of` = the POV
  sequence's new number; spatial data re-keyed) → `write_report` is given that
  precomputed spatial dict.
- Commands (`sequences.py`): at `start_tick`, `mirv_campath enabled 1`,
  `load "<abs forward-slash path>"`, `edit start` (no args = rebase the path to
  the current demo time, as observer-pack binds do), then at `end_tick`
  `enabled 0`. When any sequence is cinematic, every sequence's setup also
  runs `enabled 0` + `clear` so a path never leaks into the next clip. Plain
  runs emit no campath commands — keep it that way.
- **Not yet validated in-game:** that campath coordinates equal demo world
  coordinates (used to rank paths by distance to the kills), that `edit start`
  works from the actions file, and what the view does after a shorter path ends.

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
