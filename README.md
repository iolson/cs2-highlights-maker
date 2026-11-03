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
highlights match.dem --intro-cam face     # open each clip with a generated face cam that eases into the POV
highlights match.dem --downtime-cam random   # cut generated cams into down time between kills
highlights doctor                         # environment check
highlights update-tools                   # re-download latest HLAE/FFmpeg
```

### Compilations across demos

Rank the best moments over several demos at once:

```
highlights top a.dem b.dem c.dem --top 10
highlights top *.dem --players Alice --players Bob
```

This only prints a ranked table (no JSON report, no recording).

### Place names

Teach the tool where plays happen on each map:

```
highlights places build a.dem b.dem      # samples alive players' place names
highlights places list                   # show saved maps and cell counts
```

Run `build` once per map on a few demos (use `--every SECONDS` to sample more
or less often); results accumulate in `~/.cs2-highlights/places/<map>.json`
and label where each play happened without parsing the demo again.

The highlights table has a "Where" column with each clip's route (the places
the POV player moved through), and every sequence in the JSON report carries
`spatial.route` — `{places, text, kills}` — listing that route and where each
featured kill happened.

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
# Optional kill-feed team colors (#RRGGBB or #RRGGBBAA); leave out for the game's own.
killfeed_ct_color = "#00ACE7"
killfeed_t_color = "#F6CA26"
killfeed_plain = true   # default: transparent row backgrounds and no red local-player border
# Per-part colors ("#RRGGBB[AA]" or "R G B A"); override killfeed_plain for that part.
# killfeed_background_color = "0 0 0 0"
# killfeed_background_local_color = "0 0 0 0"
# killfeed_border_color = "255 0 0 255"

# Optional: show custom names (SteamID64 = "name") via HLAE's mirv_replace_name.
# HLAE only renames "some parts of the HUD"; check the kill feed in-game.
# The same can be done per run with `--player-name 76561198000000000="Display Name"`
# (repeatable; CLI overrides this table).
[recording.player_names]
76561198000000000 = "Display Name"

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
   provided it can cut in for at least 1.5 s, one minimum cut (`--cinematic-takes` is ignored);
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

Before recording anything, `highlights match.dem --suggest-cinematic --json-only`
previews per clip whether a cinematic is worth it, the best path, how long it
sees the player, its route, and alternatives. Then pick one with
`--cinematic --cinematic-path NAME`. The suggestions are also in the JSON
report under `cinematic_suggestion`.

Splice limits: visibility is a view-cone test against the player's position
and does not know about walls, so a cut can land on a camera that is pointed
at the player but blocked by geometry; review `splice_segments` before a long
run. The POV kill feed (which persists to the clip's end) disappears while a
cinematic cut is showing.

Notes: the path starts playing at the first recorded frame (`mirv_campath edit
start`). If it is shorter than the clip, the view returns to the POV player
when it ends, and a warning is printed. Matching assumes cam path coordinates
equal demo world coordinates, which has not been verified in-game yet.

## Generated cams (`--intro-cam`, `--downtime-cam`)

No cam path files needed: the tool computes a cam path from the demo that follows the POV
player and records it as an extra take, which is cut into the POV clip like a splice-mode
cinematic (clip length, kill times and audio are unchanged).

Styles: `face`, `profile`, `gun`, `tps` (over the shoulder), `heli` (high, looking down).
Pass one, a comma-separated list, or `random` (picked per clip, the same way on every run
of the same demo). `gun` is a wide shot 1-2 m from the player; `face` is a close-up (about 50 units).

- `--intro-cam STYLE` opens every clip (up to 3.5 s, shorter when a kill comes early) on
  the style's shot, then spirals round to just behind the player and pushes into the exact
  first-person eye pose, so the cut to the POV has no jump.
- `--downtime-cam STYLE` fills the long gaps between kills with one continuous pass of the
  style's shot (hard cut in and out, no swooping back to the POV). Only gaps of at least
  2.5 s count, each pass is up to 12 s, and at most the 2 longest gaps per clip are used, so
  a clip is not chopped up. Ranges a `--cinematic --cinematic-mode splice` cam path already
  covers are left to that path.
- A list or `random` picks one style per kind per clip (not per cut), so a clip never mixes
  cam styles.

Both can be combined with each other and with `--cinematic`; the JSON report lists the cuts
under `cinematic.generated_cams`. They are not checked against map geometry: a camera can
end up inside a wall (worst for `face`, `profile` and `tps` next to walls, and `heli`
indoors), so review each run. The offsets are estimates, tuned in
`src/highlights/recording/dyncams.py`. Not validated in-game yet: that the observed
player's model is drawn while a cam path is active, the cam path fov value, and the
hand-off to the POV.

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
