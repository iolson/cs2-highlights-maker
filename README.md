# cs2-highlights

Generate highlight videos from Counter-Strike 2 demos: feed it a match `.dem`
file, get a `*-highlights.mp4` with the best moments rendered as real
in-game footage.

```
highlights match.dem                      # best moments of the whole match
highlights match.dem --player s1mple      # one player's highlights only
highlights match.dem --players s1mple,device   # several players (comma-separated or repeated)
highlights match.dem --json-only          # analysis only, no recording
highlights match.dem --cinematic          # also re-record each play through your cam paths
highlights match.dem --cinematic --cinematic-mode splice   # cut the cinematic into the POV during down time
highlights doctor                         # environment check
highlights update-tools                   # re-download latest HLAE/FFmpeg
```

## How it works

```
match.dem → [demoparser2 parsing] → events (kills, rounds, clutches)
          → [detector] → top-N moments scored by "watchability"
          → [recording: CS2 + HLAE] → clips (frames piped straight into FFmpeg)
          → [FFmpeg] → highlights.mp4
```

Rendering video from a demo without the game is impossible, so recording uses
the same mechanism as [CS Demo Manager](https://cs-demo-manager.com) (MIT):
the game is launched through [HLAE](https://www.advancedfx.org/) with an
`AfxHookSource2.dll` injection, the CSDM server plugin executes console
commands at exact demo ticks (driven by a `<demo>.json` actions file), and
`mirv_streams` pipes frames directly into FFmpeg with no raw TGA files.

## Requirements

- Windows, CS2 installed, Steam running
- Python ≥ 3.11
- HLAE and FFmpeg — downloaded automatically on first run
- `server.dll` (the CSDM CS2 plugin) — bundled in `vendor/cs2-plugin/`, or
  picked up from an installed CS Demo Manager (see `highlights doctor`)

## Installation

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\highlights doctor
```

## What counts as a highlight

A player's kills within a round are clustered into "moments" (gap < 20 s),
and each moment gets a score: multikills (3K/4K/ACE), headshots, knife/zeus
kills, AWP noscopes, kills through smoke / while flashed / wallbangs, won 1vX
clutches (detected by tracking alive players), and round importance (pistol
rounds, overtime, final round). The top-N moments by score become clips;
overlapping moments are merged into a single clip with camera switches
between the featured players.

Scoring weights live in `src/highlights/analysis/scoring.py` (`WEIGHTS`).

## Configuration

`~\.cs2-highlights\config.toml` (everything can be overridden by CLI flags).
The folder is deliberately NOT under AppData: Microsoft Store Python
virtualizes AppData writes inside its MSIX package, making them invisible to
external processes (HLAE/CS2/FFmpeg).

```toml
[paths]
cs2_exe = "auto"      # auto: Steam registry + libraryfolders.vdf
hlae_dir = "auto"     # auto: downloaded into ~\.cs2-highlights\tools
ffmpeg_exe = "auto"
plugin_dll = "auto"   # auto: vendor/cs2-plugin, then an installed CSDM
work_dir = "auto"     # auto: ~\.cs2-highlights\work

[recording]
fps = 60
width = 1920
height = 1080
crf = 23
lead_in_seconds = 4.0
lead_out_seconds = 3.0

[selection]
top = 10
min_kills = 2
```

## Cinematic cam paths

`--cinematic` records every selected play a second time through an HLAE cam
path, so you can cut between the POV take and a cinematic take of the same
moment. The paths are **not included**: bring your own (for example a
purchased observer pack) and keep them out of git.

1. Put HLAE campath files (`.dz`, `.campath` or `.xml`) in `campaths/`, in one
   folder per map named like the map (`Nuke/` or `de_nuke/`):

   ```
   campaths/
     Nuke/
       A4.dz
       T1.dz
   ```

   `campaths/` is gitignored (apart from its README), as are `*.dz` and
   `*.campath` everywhere, so these files cannot be committed by accident.
   To use another location set `campaths_dir` under `[paths]` in the config.
2. Run `highlights match.dem --cinematic`. For each play the cam paths that
   pass closest to its kills are chosen; `--cinematic-takes 2` records the two
   best matches. Each cinematic clip follows its POV clip in the final video,
   and the JSON report marks it with `"cinematic": {"campath", "of_sequence"}`
   (its `beat_targets` entries carry `"cinematic": true`).
3. Cinematic clips show no kill feed (POV clips keep theirs).
4. `--cinematic-mode splice` instead of the default `append` produces one clip
   per play with the same length as the POV clip: the cinematic is cut in
   during down time between kills, only while its camera is pointed at the
   player. The POV footage is kept from 1 s before to 1 s after each kill
   (less when two kills are close together, so a cut still fits between
   them). Per play, the cam path that passes closest to the kills is used,
   provided it can cut in for at least 3 s (`--cinematic-takes` is ignored);
   plays no path can cover stay POV-only. The planned cuts are in the JSON
   (`cinematic.splice_segments`, clip seconds), so `--json-only` previews
   them without recording. Because lengths are unchanged, kill times and
   `beat_targets` are unaffected.
5. The console (and, for append and splice takes alike, the JSON's
   `cinematic.candidates`) lists the closest candidate paths per play with
   their distance to the kills and, in splice mode, how long each could cut
   in. To force one path for every play, use `--cinematic-path B5` (the file
   name without extension).
6. `highlights doctor` shows how many cam paths were found.

Splice limits: visibility is a view-cone test against the player's position
and does not know about walls, so a cut can land on a camera that is pointed
at the player but blocked by geometry; review `splice_segments` before a long
run. The POV kill feed (which persists to the clip's end) disappears while a
cinematic cut is showing.

Notes: the path starts playing at the first recorded frame (`mirv_campath edit
start`). If it is shorter than the clip, the view returns to the POV player
when it ends, and a warning is printed. Matching assumes cam path coordinates
equal demo world coordinates, which has not been verified in-game yet.

## Important notes

- The game is launched strictly with `-insecure` (the plugin enforces this
  too). **Never join VAC-secured servers while HLAE is running.**
- Recording runs at roughly real time: 10 clips of ~20 s each ≈ 10–15 minutes
  (game boot + demo seeks + the recording itself).
- CS2 updates regularly break HLAE injection until advancedfx ships a new
  release (usually within days). `highlights doctor` compares the CS2 build
  date against your HLAE release and tells you when to run
  `highlights update-tools`.
- FACEIT demos arrive as `.gz`, Valve ones sometimes as `.dem.zst` — both are
  decompressed automatically. CS:GO-era demos are not supported (CS2 cannot
  play them).
- Your game config is untouched: the game runs with a sandboxed settings
  folder via the `USRLOCALCSGO` environment variable.

## Attribution

The recording mechanics are ported from the
[akiver/cs-demo-manager](https://github.com/akiver/cs-demo-manager) sources
(MIT). `vendor/cs2-plugin/server.dll` is the CSDM CS2 plugin binary (MIT, see
`vendor/cs2-plugin/NOTICE`).
