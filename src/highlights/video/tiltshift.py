"""Tilt-shift look for cam-path footage: sharp band across the middle, blurred top and bottom."""

from __future__ import annotations

BLUR_SIGMA = 7.0  # gaussian blur of the out-of-focus bands
SHARP_HALF = 0.14  # half height of the fully sharp band (fraction of the frame)
FEATHER = 0.26  # height over which the blur fades in above/below the sharp band
SATURATION = 1.25  # slight colour boost sells the miniature look


def tilt_filter(
    sigma: float = BLUR_SIGMA,
    sharp_half: float = SHARP_HALF,
    feather: float = FEATHER,
    saturation: float = SATURATION,
) -> str:
    """Filter chain (input and output are the filtergraph's default pads, no labels) that blends a
    blurred copy over the frame by distance from the horizontal centre line."""
    weight = f"clip((abs(Y/H-0.5)-{sharp_half:g})/{feather:g},0,1)"
    return (
        f"split[tsharp][tblur];[tblur]gblur=sigma={sigma:g}[tblurred];"
        f"[tsharp][tblurred]blend=all_expr='A+(B-A)*{weight}',eq=saturation={saturation:g}"
    )
