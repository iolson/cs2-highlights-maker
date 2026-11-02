# Cinematic cam paths

Put your own HLAE cam path files here, one folder per map:

```
campaths/
  Nuke/      (or de_nuke/)
    A4.dz
    T1.dz
  Inferno/
    ...
```

Everything in this folder except this README is gitignored, so purchased or
otherwise licensed paths never reach the repository. Do not commit them and do
not copy them anywhere else in the repo (tests use generated paths only).

## Labels (optional)

Add a `labels.toml` next to a map's cam path files to give paths human-readable
names in the candidate list:

```
campaths/
  Nuke/
    sweep1.dz
    labels.toml
```

```toml
# labels.toml — keys are cam path file names without the extension
sweep1 = "Catwalk to A site sweep"
```

The label is shown next to the path name in the `--cinematic` candidate list.
Routes (the places each path passes through) are shown too, but only when a
place map exists — without `highlights places build`, routes are not shown.

Used by `highlights <demo> --cinematic`. See the top-level README.
