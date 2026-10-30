#!/usr/bin/env python3
"""jellyfin-splash.py — the splash on Jellyfin's login screen, from this repo.

WHY THIS EXISTS
---------------
The mission claims a branded splash ("a product splash, source SVG in
`assets/`, installed as a PNG"), and nothing in this repo ever made it:
no script rendered the SVG and no script put the result where Jellyfin reads it.
What is actually on disk on the media host by default is Jellyfin's **own
generated collage** — `SplashscreenPostScanTask` builds it from up to 30 random
posters and 30 thumbnails after *every* library scan and writes it to
`{DataPath}/splashscreen.png` (1920×1080). So the file a client shows is a poster
wall, and any hand-copied image put in its place is overwritten by the next scan.
That is the whole reason this is a script and not a one-off `cp`.

HOW JELLYFIN PICKS THE IMAGE (read off the server, not guessed)
--------------------------------------------------------------
* `BrandingOptions` (the `branding` configuration store → `{config}/branding.xml`)
  carries `SplashscreenEnabled` and `SplashscreenLocation`.
* `SplashscreenLocation` is the path of a **custom** splash. The API deliberately
  cannot set it (`BrandingOptionsDto` omits it — "prevents it from being updated
  via API", jellyfin/jellyfin#13744), so the configuration file is the only way
  to point a deployment at its own image.
* `SkiaEncoder.CreateSplashscreen()` still regenerates the collage into
  `{DataPath}/splashscreen.png`; a custom location is what makes that file
  irrelevant — **but only a location that exists**. A `SplashscreenLocation`
  naming a path that is not there is not an error the server reports: it serves
  `{DataPath}/splashscreen.png` instead, which is the collage again. The first
  version of this script recorded `/config/data/splash.png` while installing to
  `/config/data/data/splash.png` (this image's data dir is
  `{config}/data/data`), and the measurement that caught it was putting a
  *different* image in the collage's place and watching which one came back from
  `/Branding/Splashscreen`. `--check` now maps the recorded path back through the
  appdata mount and fails when no file is there.

So `--apply` does two things, and both are needed: it puts the rendered image in
the data directory under a name Jellyfin never writes, and it records that path
in `branding.xml` as the custom splash. It also leaves a copy at
`{DataPath}/splashscreen.png`, because that is the file the collage overwrites
and the file a client shows when the location is not honoured.

THE ART (2026-10 redesign)
--------------------------
One artwork serves three surfaces, because Jellyfin has exactly one splash
setting: the **web login screen** (`/Branding/Splashscreen` before a session),
the **TV client** (Android TV / Roku / Fire TV draw the same endpoint on launch),
and the **collage file** `splashscreen.png` that a client falls back to.

The design is *Celestial Cinema*: an obsidian field of deterministic stars and
sparkles; a film-strip perforation band along the top and bottom edges; an
aperture iris whose twelve blades close around a golden play mark, ringed by a
72-tick measurement bezel, fine orbit paths with two satellites, and a 48-ray
crown; mirrored equaliser wings flanking the wordmark; corner brackets. Nothing
in it is photographic, so the PNG is a few hundred kilobytes instead of the
collage's four megabytes, and nothing is random at render time — the same plan
always produces the same bytes.

The renderer is expressed as a **drawing plan** (a list of primitives) with two
backends: Pillow, and an SVG writer. `--render` writes the 4K PNG, `--svg`
writes `assets/monarch-splash.svg`, and both come from the same list, so the
committed design cannot drift from the installed pixels. Rendering needs
Pillow, which is why the PNG is committed beside the SVG: `--check` and
`--apply` are stdlib-only and work on a host with no rasterizer at all.

USAGE
-----
    python3 scripts/jellyfin-splash.py --check        # report only (default)
    python3 scripts/jellyfin-splash.py --apply        # install + point branding at it
    python3 scripts/jellyfin-splash.py --render       # re-render assets/monarch-splash.png (needs Pillow)
    python3 scripts/jellyfin-splash.py --svg          # rewrite assets/monarch-splash.svg
    python3 scripts/jellyfin-splash.py --apply --variant light
    python3 scripts/jellyfin-splash.py --render --scale 1     # assets/monarch-splash-1080.png
    python3 scripts/jellyfin-splash.py --apply --scale 1      # install the 1080p render instead

Runs on a host that mounts the media host's appdata (`APPDATA`, default
`/docker/appdata`), or inside a container that has it. Paths:

    asset           assets/monarch-splash.png                 (this repo, 3840×2160)
    config          $APPDATA/jellyfin/branding.xml            (Jellyfin's /config)
    installed       $APPDATA/jellyfin/data/data/monarch-splash.png
                    recorded in branding.xml as /config/data/data/monarch-splash.png
                    (the *container* path Jellyfin resolves, not the host one)

The installed draw is 4K by default because a TV client upscales 1080p visibly on
a 4K panel, and because the same file is what a 4K web session paints behind the
login card. `--scale 1` renders and installs the same design at 1920×1080 under
its own name (`monarch-splash-1080.png`), for a client that caps the size of a
splash image; `--check` reads which of the two names `branding.xml` carries, so a
host that deliberately installed the smaller one is not reported as drifted.

Exit codes: 0 = the branded splash is installed and wired; 1 = it is not (a
finding is printed); 2 = cannot run (no asset and no Pillow, or no appdata).
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = REPO_ROOT / "assets"

# The design is laid out on a 1920×1080 grid and rendered at 2× (4K). Every
# coordinate below is in that grid.
WIDTH, HEIGHT = 1920, 1080
SCALE = 2
SPLASH_SIZE = (WIDTH * SCALE, HEIGHT * SCALE)

# Both renders of every variant, and the suffix that tells them apart. 4K is what
# --apply installs unless told otherwise; `-1080` is the same drawing for a client
# that caps splash size (a 4K PNG is four times the bytes a 1080p surface can use,
# on a launch where the client is already waiting). The two never share a name, so
# a host carrying either one can be judged without guessing.
RESOLUTION_SIZE = {2: (WIDTH * 2, HEIGHT * 2), 1: (WIDTH, HEIGHT)}
RESOLUTION_SUFFIX = {2: "", 1: "-1080"}

# Geometry, shared by both variants and by both backends. Counts are here rather
# than inline because the tests pin them: the motif is a measurement instrument
# as much as a picture, and a missing tick row is a design change, not a detail.
GEOMETRY = {
    "emblem": {"cx": 960, "cy": 372, "r": 150, "blades": 12, "rays": 48},
    "rings": (214, 236, 296, 326),
    "ticks": {"r0": 244, "r1": 274, "long_to": 288, "count": 72, "long_every": 6},
    "orbits": ((430, 168, -15.0), (468, 206, 21.0)),
    "filmstrip": {"rows": (40, 1040), "pitch": 72, "hole": (28, 14), "radius": 4},
    "wave": {"left": 352, "right": 1568, "bars": 21, "pitch": 13, "base": 878,
             "height": 74},
    "brackets": {"inset": 60, "arm": 92},
    "stars": 170,
    "sparkles": 9,
}

# Per-variant paint. `gold` is the gradient used for the emblem's play mark and
# the wordmark accents; `line` is every hairline; the alphas scale each layer's
# weight so the light theme is the same drawing in engraver's ink, not a filter.
DESIGN = {
    "dark": {
        "asset": "monarch-splash.png",
        "svg": "monarch-splash.svg",
        "bg": ((0.0, "#06070a"), (0.52, "#101319"), (1.0, "#04050a")),
        "bloom": ("#d8b56d", 0.12),
        "bloom_cool": ("#3d5b91", 0.10),
        "gold": ((0.0, "#f8e3ab"), (0.42, "#e2c07a"), (0.72, "#caa35d"), (1.0, "#8a6733")),
        "line": "#d8b56d",
        "ink": "#f6f1e6",
        "mid": "#cfae73",
        "muted": "#9d988e",
        "alphas": {
            "rings": 0.30, "ticks": 0.30, "orbits": 0.20, "filmstrip": 0.16,
            "stars": 0.62, "sparkles": 0.70, "wave": 0.62, "brackets": 0.42,
            "rays": 0.34, "blades": 0.12, "play_glow": 0.10,
        },
    },
    "light": {
        "asset": "monarch-splash-light.png",
        "svg": "monarch-splash-light.svg",
        "bg": ((0.0, "#fbf8f0"), (1.0, "#ece3d1")),
        "bloom": ("#caa35d", 0.10),
        "bloom_cool": ("#caa35d", 0.0),
        "gold": ((0.0, "#6e5228"), (0.5, "#a9854a"), (1.0, "#6e5228")),
        "line": "#8e6b36",
        "ink": "#221f1a",
        "mid": "#8e6b36",
        "muted": "#6c655a",
        "alphas": {
            "rings": 0.55, "ticks": 0.38, "orbits": 0.26, "filmstrip": 0.12,
            "stars": 0.30, "sparkles": 0.38, "wave": 0.50, "brackets": 0.55,
            "rays": 0.34, "blades": 0.10, "play_glow": 0.0,
        },
    },
}

# Text runs, baseline-relative, in the 1920×1080 grid.
TEXT = (
    {"string": "MONARCH", "y": 812, "size": 104, "tracking": 26,
     "colour": "ink", "font": "serif"},
    {"string": "FILM · SERIES · LIVE TELEVISION", "y": 866, "size": 21,
     "tracking": 11, "colour": "mid", "font": "sans-semibold"},
    {"string": "EVERY FRAME, IN ITS PLACE", "y": 936, "size": 17,
     "tracking": 9, "colour": "muted", "font": "sans"},
)

FONTS = {
    "serif": ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
              "/usr/share/fonts/truetype/freefont/FreeSerif.ttf"),
    "sans": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
             "/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
    "sans-semibold": ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                      "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                      "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
}

BRANDING_FILE = "branding.xml"
SPLASH_ASSET = ASSET_DIR / "monarch-splash.png"
INSTALLED_NAME = "monarch-splash.png"


def asset_name(variant: str, scale: int = SCALE) -> str:
    """`monarch-splash.png` at 4K, `monarch-splash-1080.png` at 1080p."""
    stem = DESIGN[variant]["asset"][: -len(".png")]
    return f"{stem}{RESOLUTION_SUFFIX[scale]}.png"


def scale_of_asset(name: str) -> int | None:
    """The scale a `-1080` (or absent) suffix implies, or None for a name this
    script does not install (Jellyfin's own `splashscreen.png`, an operator's
    hand-placed file) — in which case the caller falls back to `--scale`."""
    for scale in RESOLUTION_SUFFIX:
        if any(name == asset_name(variant, scale) for variant in DESIGN):
            return scale
    return None
# The path recorded in branding.xml is resolved by the *server*, i.e. inside the
# Jellyfin container, where the appdata mount is /config and the data dir is
# {config}/data/data — the directory the generated collage and jellyfin.db live
# in. It has to be the SAME directory --data-path installs into: a location
# naming somewhere else is not an error to the server, it just quietly serves the
# collage instead (see the docstring).
DEFAULT_CONTAINER_DATA = "/config/data/data"
DEFAULT_CONTAINER_CONFIG = "/config"


class CannotRun(RuntimeError):
    """Not enough on disk (or no rasterizer) to do the job."""


# ── PNG facts, without Pillow ───────────────────────────────────────────────

def png_dimensions(path: Path) -> tuple[int, int]:
    """(width, height) of a PNG, from its IHDR — the whole file is not read."""
    with open(path, "rb") as fh:
        header = fh.read(26)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise CannotRun(f"{path} is not a PNG (bad signature or IHDR)")
    return struct.unpack(">II", header[16:24])


def container_path_to_host(container_path: str, config_dir: Path,
                           container_config: str) -> Path | None:
    """The host path a container path under the appdata mount maps to, or None.

    This is the assertion the first version of this script was missing. A
    `SplashscreenLocation` is resolved *inside the container*, so the only way to
    judge it from out here is to map it back through the mount it claims and look
    for the file. `None` means the path is not under that mount at all (a layout
    this script cannot reason about — reported, not guessed at).
    """
    prefix = container_config.rstrip("/") + "/"
    if not container_path.startswith(prefix):
        return None
    return config_dir / container_path[len(prefix):]


def same_bytes(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                chunk_a, chunk_b = fa.read(1 << 20), fb.read(1 << 20)
                if chunk_a != chunk_b:
                    return False
                if not chunk_a:
                    return True
    except OSError:
        return False


# ── colours ─────────────────────────────────────────────────────────────────

def _rgb(colour: str) -> tuple[int, int, int]:
    colour = colour.lstrip("#")
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))


def _lerp_stops(stops, t: float) -> tuple[float, float, float]:
    t = min(max(t, 0.0), 1.0)
    for (offset, colour), (next_offset, next_colour) in zip(stops, stops[1:]):
        if t <= next_offset:
            span = (next_offset - offset) or 1.0
            k = (t - offset) / span
            a, b = _rgb(colour), _rgb(next_colour)
            return tuple(a[i] + (b[i] - a[i]) * k for i in range(3))
    return tuple(float(c) for c in _rgb(stops[-1][1]))


def _gradient_at(stops, t: float) -> str:
    return "#%02x%02x%02x" % tuple(int(c) for c in _lerp_stops(stops, t))


# ── the drawing plan ────────────────────────────────────────────────────────

def _lcg(seed: int):
    """A tiny deterministic generator (no `random` state, no host entropy)."""
    state = seed & 0xFFFFFFFF

    def nxt() -> float:
        nonlocal state
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF

    return nxt


def _rotated_ellipse(cx: float, cy: float, rx: float, ry: float, angle: float,
                     steps: int = 180):
    cos_a, sin_a = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    points = []
    for index in range(steps + 1):
        t = 2 * math.pi * index / steps
        x, y = rx * math.cos(t), ry * math.sin(t)
        points.append((cx + x * cos_a - y * sin_a, cy + x * sin_a + y * cos_a))
    return points


def _aperture_blades(cx: float, cy: float, r_in: float, r_out: float, count: int):
    """The iris: `count` wedges, each an arc at `r_in` closed out to `r_out`."""
    blades = []
    for index in range(count):
        a0 = 2 * math.pi * index / count - math.pi / 2
        a1 = 2 * math.pi * (index + 1) / count - math.pi / 2
        inner = [(cx + r_in * math.cos(a0 + (a1 - a0) * k / 8),
                  cy + r_in * math.sin(a0 + (a1 - a0) * k / 8)) for k in range(9)]
        outer = [(cx + r_out * math.cos(a1 - (a1 - a0) * k / 8),
                  cy + r_out * math.sin(a1 - (a1 - a0) * k / 8)) for k in range(9)]
        blades.append(inner + outer)
    return blades


def build_plan(variant: str) -> list[dict]:
    """Every primitive, in paint order. Both backends draw this list verbatim."""
    design = DESIGN[variant]
    geo = GEOMETRY
    alpha = design["alphas"]
    emblem = geo["emblem"]
    cx, cy, r = emblem["cx"], emblem["cy"], emblem["r"]
    ops: list[dict] = []

    # Two washes: warm behind the emblem, a cool one low-left for depth.
    ops.append({"t": "bloom", "cx": cx, "cy": cy - 30, "rx": 760, "ry": 620,
                "colour": design["bloom"][0], "alpha": design["bloom"][1]})
    if design["bloom_cool"][1] > 0:
        ops.append({"t": "bloom", "cx": 250, "cy": 1010, "rx": 720, "ry": 540,
                    "colour": design["bloom_cool"][0], "alpha": design["bloom_cool"][1]})

    # Star field, then the handful of four-point sparkles that read at TV
    # distance. Seeded, so a re-render is byte-identical.
    rand = _lcg(20261029)
    for _ in range(geo["stars"]):
        x, y = rand() * WIDTH, rand() * HEIGHT
        radius = 0.45 + rand() * 1.35
        ops.append({"t": "dot", "cx": x, "cy": y, "r": radius,
                    "colour": design["line"], "alpha": alpha["stars"] * (0.35 + rand() * 0.65)})
    rand = _lcg(971)
    for _ in range(geo["sparkles"]):
        x, y = 120 + rand() * (WIDTH - 240), 120 + rand() * (HEIGHT - 240)
        size = 4 + rand() * 6
        a = alpha["sparkles"] * (0.5 + rand() * 0.5)
        for dx, dy in ((size, 0), (-size, 0), (0, size), (0, -size)):
            ops.append({"t": "line", "x0": x - dx, "y0": y - dy, "x1": x + dx,
                        "y1": y + dy, "colour": design["line"], "width": 1, "alpha": a})
        ops.append({"t": "dot", "cx": x, "cy": y, "r": 1.4,
                    "colour": design["line"], "alpha": min(1.0, a * 1.6)})

    # Film-strip bands: the media motif at the very top and bottom edge.
    strip = geo["filmstrip"]
    hole_w, hole_h = strip["hole"]
    for row in strip["rows"]:
        for x in range(strip["pitch"] // 2, WIDTH + strip["pitch"], strip["pitch"]):
            ops.append({"t": "round_rect", "x0": x - hole_w / 2, "y0": row - hole_h / 2,
                        "x1": x + hole_w / 2, "y1": row + hole_h / 2,
                        "radius": strip["radius"], "stroke": design["line"],
                        "width": 1, "alpha": alpha["filmstrip"]})
        ops.append({"t": "line", "x0": 0, "y0": row + 26, "x1": WIDTH, "y1": row + 26,
                    "colour": design["line"], "width": 1, "alpha": alpha["filmstrip"] * 0.5})

    # Corner brackets: two strokes and a diamond, at all four corners.
    inset, arm = geo["brackets"]["inset"], geo["brackets"]["arm"]
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        x = inset if sx > 0 else WIDTH - inset
        y = inset + 14 if sy > 0 else HEIGHT - inset - 14
        ops.append({"t": "line", "x0": x, "y0": y, "x1": x, "y1": y + sy * arm,
                    "colour": design["line"], "width": 2, "alpha": alpha["brackets"]})
        ops.append({"t": "line", "x0": x, "y0": y, "x1": x + sx * arm, "y1": y,
                    "colour": design["line"], "width": 2, "alpha": alpha["brackets"]})
        ops.append({"t": "line", "x0": x + sx * 8, "y0": y + sy * 8,
                    "x1": x + sx * (arm * 0.6), "y1": y + sy * 8,
                    "colour": design["line"], "width": 1, "alpha": alpha["brackets"] * 0.6})
        ops.append({"t": "line", "x0": x + sx * 8, "y0": y + sy * 8,
                    "x1": x + sx * 8, "y1": y + sy * (arm * 0.6),
                    "colour": design["line"], "width": 1, "alpha": alpha["brackets"] * 0.6})
        ops.append({"t": "diamond", "cx": x + sx * 12, "cy": y + sy * 12, "r": 4,
                    "colour": design["line"], "alpha": alpha["brackets"]})

    # Orbit paths and their satellites, drawn under the bezel.
    for index, (rx, ry, angle) in enumerate(geo["orbits"]):
        ops.append({"t": "polyline", "points": _rotated_ellipse(cx, cy, rx, ry, angle),
                    "stroke": design["line"], "width": 1,
                    "alpha": alpha["orbits"] * (1.0 if index == 0 else 0.65)})
        sx = cx + rx * math.cos(math.radians(angle + 32))
        sy = cy + ry * math.sin(math.radians(angle + 32))
        ops.append({"t": "dot", "cx": sx, "cy": sy, "r": 3.2,
                    "colour": design["mid"], "alpha": min(1.0, alpha["orbits"] * 3.2)})

    # The bezel: four rings, the 72-tick measurement band, and a ray crown.
    for index, radius in enumerate(geo["rings"]):
        ops.append({"t": "circle", "cx": cx, "cy": cy, "r": radius,
                    "stroke": design["line"], "width": 1,
                    "alpha": alpha["rings"] * (1.0 if index < 2 else 0.55)})
    ticks = geo["ticks"]
    for index in range(ticks["count"]):
        angle = 2 * math.pi * index / ticks["count"] - math.pi / 2
        outer = ticks["long_to"] if index % ticks["long_every"] == 0 else ticks["r1"]
        ops.append({"t": "line",
                    "x0": cx + ticks["r0"] * math.cos(angle),
                    "y0": cy + ticks["r0"] * math.sin(angle),
                    "x1": cx + outer * math.cos(angle),
                    "y1": cy + outer * math.sin(angle),
                    "colour": design["line"], "width": 1, "alpha": alpha["ticks"]})
    for index in range(emblem["rays"]):
        angle = 2 * math.pi * index / emblem["rays"] - math.pi / 2
        inner = 334 if index % 2 == 0 else 338
        outer = 350 if index % 2 == 0 else 344
        ops.append({"t": "line",
                    "x0": cx + inner * math.cos(angle), "y0": cy + inner * math.sin(angle),
                    "x1": cx + outer * math.cos(angle), "y1": cy + outer * math.sin(angle),
                    "colour": design["line"], "width": 1, "alpha": alpha["rays"]})

    # The iris: soft halo, twelve blades, inner rings, the play mark.
    if alpha["play_glow"] > 0:
        ops.append({"t": "glow", "cx": cx, "cy": cy, "r": r * 1.35,
                    "colour": design["gold"][1][1], "alpha": alpha["play_glow"]})
    for index, blade in enumerate(_aperture_blades(cx, cy, 96, r, emblem["blades"])):
        ops.append({"t": "polygon", "points": blade, "fill": design["gold"][2][1],
                    "alpha": alpha["blades"] * (1.0 if index % 2 == 0 else 0.5)})
    ops.append({"t": "circle", "cx": cx, "cy": cy, "r": r, "stroke": design["gold"][1][1],
                "width": 2, "alpha": 0.95})
    ops.append({"t": "circle", "cx": cx, "cy": cy, "r": r - 8, "stroke": design["line"],
                "width": 1, "alpha": 0.45})
    ops.append({"t": "circle", "cx": cx, "cy": cy, "r": 92, "stroke": design["line"],
                "width": 1, "alpha": 0.55})
    ops.append({"t": "circle", "cx": cx, "cy": cy, "r": 84, "stroke": design["line"],
                "width": 1, "alpha": 0.22})

    # The play mark: a triangle with a darker lower facet for depth.
    play = [(cx - 30, cy - 44), (cx + 52, cy), (cx - 30, cy + 44)]
    ops.append({"t": "polygon", "points": play, "fill": design["gold"][0][1],
                "alpha": 1.0})
    ops.append({"t": "polygon",
                "points": [(cx - 30, cy - 4), (cx + 52, cy), (cx - 30, cy + 44)],
                "fill": design["gold"][2][1], "alpha": 0.55})
    ops.append({"t": "polyline", "points": play + [play[0]], "stroke": design["gold"][-1][1],
                "width": 2, "alpha": 0.9})

    # Equaliser wings flanking the wordmark: mirrored bars, tallest inboard.
    wave = geo["wave"]
    for side in (-1, 1):
        for index in range(wave["bars"]):
            x = (wave["left"] if side < 0 else wave["right"]) + side * index * wave["pitch"]
            k = 1 - index / wave["bars"]
            height = wave["height"] * (0.18 + 0.82 * abs(math.sin((index + 1) * 0.62))) * (0.35 + 0.65 * k)
            ops.append({"t": "line", "x0": x, "y0": wave["base"], "x1": x,
                        "y1": wave["base"] - height, "colour": design["line"],
                        "width": 3, "alpha": alpha["wave"] * (0.45 + 0.55 * k)})
            if index % 4 == 0:
                ops.append({"t": "dot", "cx": x, "cy": wave["base"] - height - 6, "r": 2,
                            "colour": design["line"], "alpha": alpha["wave"] * 0.8})

    # Rules above and below the strapline, with end ticks.
    for y, half, a in ((900, 210, 0.65), (958, 130, 0.30)):
        ops.append({"t": "line", "x0": cx - half, "y0": y, "x1": cx + half, "y1": y,
                    "colour": design["line"], "width": 1, "alpha": a})
        for end in (-half, half):
            ops.append({"t": "line", "x0": cx + end, "y0": y - 5, "x1": cx + end, "y1": y + 5,
                        "colour": design["line"], "width": 1, "alpha": a})

    # Text last: it must never be crossed by a hairline.
    for spec in TEXT:
        ops.append({"t": "text", "string": spec["string"], "x": cx, "y": spec["y"],
                    "size": spec["size"], "tracking": spec["tracking"],
                    "colour": design[spec["colour"]], "font": spec["font"]})
    return ops


# ── SVG backend ─────────────────────────────────────────────────────────────

def _svg_paint(op: dict) -> str:
    if op.get("fill"):
        return f'fill="{op["fill"]}" stroke="none"'
    return f'stroke="{op["stroke"]}" fill="none" stroke-width="{max(1, round(op["width"]))}"'


def svg(variant: str) -> str:
    """The committed design-of-record, drawn from the same plan as the PNG."""
    design = DESIGN[variant]
    ops = build_plan(variant)
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc">',
        '  <title id="title">Monarch Media</title>',
        '  <desc id="desc">Celestial Cinema: an aperture iris closing on a golden play mark, '
        'inside a tick bezel and orbit paths, over an obsidian starfield with film-strip '
        'perforations and equaliser wings.</desc>',
        "  <defs>",
        '    <linearGradient id="field" x1="0" y1="0" x2="1" y2="1">'
        + "".join(f'<stop offset="{o}" stop-color="{c}"/>' for o, c in design["bg"])
        + "</linearGradient>",
        '    <linearGradient id="gold" x1="0" y1="0" x2="1" y2="1">'
        + "".join(f'<stop offset="{o}" stop-color="{c}"/>' for o, c in design["gold"])
        + "</linearGradient>",
        "  </defs>",
        f'  <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#field)"/>',
    ]
    bloom_index = 0
    bloom_defs: list[str] = []
    for op in ops:
        a = round(op.get("alpha", 1.0), 3)
        if op["t"] == "bloom":
            bloom_index += 1
            bloom_defs.append(
                f'    <radialGradient id="bloom{bloom_index}">'
                f'<stop stop-color="{op["colour"]}" stop-opacity="{a}"/>'
                f'<stop offset="1" stop-color="{op["colour"]}" stop-opacity="0"/>'
                "</radialGradient>")
            lines.append(f'  <ellipse cx="{op["cx"]:.0f}" cy="{op["cy"]:.0f}" '
                         f'rx="{op["rx"]:.0f}" ry="{op["ry"]:.0f}" fill="url(#bloom{bloom_index})"/>')
        elif op["t"] == "glow":
            lines.append(f'  <circle cx="{op["cx"]:.0f}" cy="{op["cy"]:.0f}" r="{op["r"]:.0f}" '
                         f'fill="{op["colour"]}" opacity="{a}" filter="blur(24)"/>')
        elif op["t"] == "dot":
            lines.append(f'  <circle cx="{op["cx"]:.1f}" cy="{op["cy"]:.1f}" '
                         f'r="{op["r"]:.2f}" fill="{op["colour"]}" opacity="{a}"/>')
        elif op["t"] == "circle":
            lines.append(f'  <circle cx="{op["cx"]:.0f}" cy="{op["cy"]:.0f}" r="{op["r"]:.0f}" '
                         f'stroke="{op["stroke"]}" fill="none" '
                         f'stroke-width="{max(1, round(op["width"]))}" opacity="{a}"/>')
        elif op["t"] == "line":
            lines.append(f'  <line x1="{op["x0"]:.1f}" y1="{op["y0"]:.1f}" '
                         f'x2="{op["x1"]:.1f}" y2="{op["y1"]:.1f}" '
                         f'stroke="{op["colour"]}" stroke-width="{max(1, round(op["width"]))}" '
                         f'opacity="{a}"/>')
        elif op["t"] in ("polygon", "polyline"):
            tag = "polygon" if op["t"] == "polygon" else "polyline"
            points = " ".join(f"{x:.1f},{y:.1f}" for x, y in op["points"])
            lines.append(f'  <{tag} points="{points}" {_svg_paint(op)} opacity="{a}"/>')
        elif op["t"] == "round_rect":
            lines.append(f'  <rect x="{op["x0"]:.1f}" y="{op["y0"]:.1f}" '
                         f'width="{op["x1"] - op["x0"]:.1f}" height="{op["y1"] - op["y0"]:.1f}" '
                         f'rx="{op["radius"]}" fill="none" stroke="{op["stroke"]}" '
                         f'stroke-width="{max(1, round(op["width"]))}" opacity="{a}"/>')
        elif op["t"] == "diamond":
            x, y, d = op["cx"], op["cy"], op["r"]
            lines.append(f'  <polygon points="{x:.1f},{y - d:.1f} {x + d:.1f},{y:.1f} '
                         f'{x:.1f},{y + d:.1f} {x - d:.1f},{y:.1f}" '
                         f'fill="{op["colour"]}" opacity="{a}"/>')
        elif op["t"] == "text":
            family = ("Georgia,Times New Roman,serif" if op["font"] == "serif"
                      else "Inter,Segoe UI,Arial,sans-serif")
            weight = "600" if op["font"] == "sans-semibold" else "400"
            lines.append(f'  <text x="{op["x"]}" y="{op["y"]}" text-anchor="middle" '
                         f'fill="{op["colour"]}" font-family="{family}" '
                         f'font-size="{op["size"]}" font-weight="{weight}" '
                         f'letter-spacing="{op["tracking"]}">{op["string"]}</text>')
    lines[lines.index("  </defs>"):lines.index("  </defs>")] = bloom_defs
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


# ── Pillow backend ──────────────────────────────────────────────────────────

def _font_path(kind: str, override: str) -> str:
    path = override or next((p for p in FONTS[kind] if os.path.exists(p)), "")
    if not path or not os.path.exists(path):
        raise CannotRun(
            f"no {kind} font found on this host (looked in {FONTS[kind]}); "
            "pass --font-serif/--font-sans")
    return path


def render(variant: str, out_path: Path, font_serif: str = "", font_sans: str = "",
           size: tuple[int, int] = SPLASH_SIZE) -> Path:
    """Render the plan to `out_path` at `size` (default 4K). Needs Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
    except ImportError as error:  # pragma: no cover - host without Pillow
        raise CannotRun(
            f"Pillow is required to render the splash ({error}). Either install "
            "python3-pil, or use the committed asset: --check/--apply need no "
            "rasterizer.") from None

    design = DESIGN[variant]
    k = size[0] / WIDTH
    if abs(size[1] / HEIGHT - k) > 1e-9:
        raise CannotRun("the splash is 16:9; pass a size that keeps that ratio")

    def s(value: float) -> float:
        return value * k

    def w(value: float) -> int:
        return max(1, int(round(value * k)))

    image = Image.new("RGB", size, _rgb(design["bg"][0][1]))

    # Background: sampled small, scaled up — a smooth ramp survives bilinear
    # scaling, and per-pixel work over 8M pixels in Python does not.
    small = Image.new("RGB", (192, 108))
    for y in range(108):
        for x in range(192):
            t = (x / 191 + y / 107) / 2
            small.putpixel((x, y), tuple(int(c) for c in _lerp_stops(design["bg"], t)))
    image.paste(small.resize(size, Image.BILINEAR), (0, 0))

    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    def paint(colour: str, alpha: float) -> tuple[int, int, int, int]:
        r, g, b = _rgb(colour)
        return (r, g, b, int(round(255 * min(max(alpha, 0.0), 1.0))))

    for op in build_plan(variant):
        t = op["t"]
        a = op.get("alpha", 1.0)
        if t == "bloom":
            layer = Image.new("RGBA", size, (0, 0, 0, 0))
            halo = ImageDraw.Draw(layer)
            colour = _rgb(op["colour"])
            steps = 48
            for index in range(steps, 0, -1):
                f = index / steps
                halo.ellipse((s(op["cx"] - op["rx"] * f), s(op["cy"] - op["ry"] * f),
                              s(op["cx"] + op["rx"] * f), s(op["cy"] + op["ry"] * f)),
                             fill=(*colour, int(255 * a * (1 - f) ** 1.6)))
            overlay = Image.alpha_composite(overlay, layer)
            draw = ImageDraw.Draw(overlay)
        elif t == "glow":
            layer = Image.new("RGBA", size, (0, 0, 0, 0))
            ImageDraw.Draw(layer).ellipse(
                (s(op["cx"] - op["r"]), s(op["cy"] - op["r"]),
                 s(op["cx"] + op["r"]), s(op["cy"] + op["r"])),
                fill=paint(op["colour"], a))
            overlay = Image.alpha_composite(
                overlay, layer.filter(ImageFilter.GaussianBlur(s(26))))
            draw = ImageDraw.Draw(overlay)
        elif t == "dot":
            draw.ellipse((s(op["cx"] - op["r"]), s(op["cy"] - op["r"]),
                          s(op["cx"] + op["r"]), s(op["cy"] + op["r"])),
                         fill=paint(op["colour"], a))
        elif t == "circle":
            draw.ellipse((s(op["cx"] - op["r"]), s(op["cy"] - op["r"]),
                          s(op["cx"] + op["r"]), s(op["cy"] + op["r"])),
                         outline=paint(op["stroke"], a), width=w(op["width"]))
        elif t == "line":
            draw.line([(s(op["x0"]), s(op["y0"])), (s(op["x1"]), s(op["y1"]))],
                      fill=paint(op["colour"], a), width=w(op["width"]))
        elif t == "polygon":
            draw.polygon([(s(x), s(y)) for x, y in op["points"]],
                         fill=paint(op["fill"], a))
        elif t == "polyline":
            draw.line([(s(x), s(y)) for x, y in op["points"]],
                      fill=paint(op["stroke"], a), width=w(op["width"]), joint="curve")
        elif t == "round_rect":
            draw.rounded_rectangle((s(op["x0"]), s(op["y0"]), s(op["x1"]), s(op["y1"])),
                                   radius=s(op["radius"]), outline=paint(op["stroke"], a),
                                   width=w(op["width"]))
        elif t == "diamond":
            x, y, d = op["cx"], op["cy"], op["r"]
            draw.polygon([(s(x), s(y - d)), (s(x + d), s(y)),
                          (s(x), s(y + d)), (s(x - d), s(y))], fill=paint(op["colour"], a))

    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

    # Text: each glyph placed with the SVG's tracking, so the PNG and the SVG
    # centre the same way.
    text_layer = Image.new("RGBA", size, (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_layer)
    for op in build_plan(variant):
        if op["t"] != "text":
            continue
        path = _font_path(op["font"], font_serif if op["font"] == "serif" else font_sans)
        font = ImageFont.truetype(path, int(round(op["size"] * k)))
        tracking = op["tracking"] * k
        advances = [text_draw.textlength(ch, font=font) for ch in op["string"]]
        total = sum(advances) + tracking * (len(op["string"]) - 1)
        x = s(op["x"]) - total / 2
        for ch, advance in zip(op["string"], advances):
            text_draw.text((x, s(op["y"])), ch, font=font, fill=paint(op["colour"], 1.0),
                           anchor="ls")
            x += advance + tracking
    image = Image.alpha_composite(image.convert("RGBA"), text_layer).convert("RGB")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, "PNG", optimize=True)
    return out_path


# ── Jellyfin's branding configuration ───────────────────────────────────────

def read_branding(config_dir: Path) -> dict:
    """The `branding` configuration store, as (enabled, location)."""
    path = config_dir / BRANDING_FILE
    if not path.is_file():
        return {"path": path, "enabled": False, "location": "", "exists": False}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        raise CannotRun(f"cannot read {path} ({error})") from None
    enabled = (root.findtext("SplashscreenEnabled") or "").strip().lower() == "true"
    return {"path": path, "enabled": enabled,
            "location": (root.findtext("SplashscreenLocation") or "").strip(), "exists": True}


def write_branding(config_dir: Path, location: str) -> Path:
    """Set the custom splash (and enable it), preserving every other key.

    A wholesale rewrite would drop `LoginDisclaimer`/`CustomCss` — and, per
    jellyfin/jellyfin#13744, an API-side branding write is exactly what used to
    erase this path, so this file is written whole and only these two keys move.
    """
    path = config_dir / BRANDING_FILE
    if path.is_file():
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            root = ET.Element("BrandingOptions")
    else:
        root = ET.Element("BrandingOptions", {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xmlns:xsd": "http://www.w3.org/2001/XMLSchema",
        })
    for key, value in (("SplashscreenEnabled", "true"), ("SplashscreenLocation", location)):
        node = root.find(key)
        if node is None:
            node = ET.SubElement(root, key)
        node.text = value
    config_dir.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


# ── the check ───────────────────────────────────────────────────────────────

def check(args) -> int:
    # No appdata at all is not a finding, it is "cannot look": "the splash is
    # missing" and "this is not the host that has the media stack" are different
    # facts, and only one of them is worth an operator's time.
    config_dir = Path(args.config_dir)
    data_dir = Path(args.data_path)
    if not config_dir.is_dir() or not data_dir.is_dir():
        raise CannotRun(
            f"{config_dir} / {data_dir} do not exist — is this the media host's appdata? "
            "(set APPDATA, or pass --appdata/--data-path)")

    findings = []
    branding = read_branding(config_dir)
    location = branding["location"]
    # Which of the two renders this host is meant to be carrying: the name in
    # branding.xml decides, so `--check` with no arguments judges a 1080p install
    # as a 1080p install rather than as a 4K one that drifted.
    scale = scale_of_asset(Path(location).name)
    if scale is None:
        scale = args.scale
    installed_name = asset_name(args.variant, scale)
    expected_size = RESOLUTION_SIZE[scale]
    if not args.asset_given:
        args.asset = str(ASSET_DIR / installed_name)
    asset = Path(args.asset)
    if not asset.is_file():
        findings.append(
            f"{asset} is missing — render it with --render (needs Pillow), or "
            "restore the committed asset")
    else:
        try:
            width, height = png_dimensions(asset)
        except CannotRun as error:
            findings.append(str(error))
        else:
            if (width, height) != expected_size:
                findings.append(f"{asset} is {width}×{height}, expected "
                                f"{expected_size[0]}×{expected_size[1]}")

    installed = data_dir / installed_name
    if not installed.is_file():
        findings.append(f"{installed} is missing — the branded splash is not installed")
    elif asset.is_file() and not same_bytes(installed, asset):
        findings.append(f"{installed} is not {asset.name} — it has been replaced")

    expected = f"{args.container_data_path.rstrip('/')}/{installed_name}"
    if not branding["exists"]:
        findings.append(f"{branding['path']} does not exist — Jellyfin has no custom splash "
                        "and serves the image its own post-scan task generates")
    else:
        if not branding["enabled"]:
            findings.append(f"{branding['path']} has SplashscreenEnabled=false — the splash "
                            "is switched off")
        if location != expected:
            findings.append(f"{branding['path']} names {location or '(nothing)'} as the "
                            f"SplashscreenLocation, expected {expected}")
        elif location:
            # The recorded path is only worth anything if a file is at it: the
            # server does not fail on a location it cannot read, it serves the
            # generated collage instead.
            mapped = container_path_to_host(location, Path(args.config_dir),
                                            args.container_config_path)
            if mapped is None:
                findings.append(f"the SplashscreenLocation {location!r} is not under the "
                                f"appdata mount {args.container_config_path!r} this check maps "
                                "through — is --container-config-path right?")
            elif not mapped.is_file():
                findings.append(f"the SplashscreenLocation {location} maps to {mapped} on this "
                                "host and no file is there — Jellyfin does not report a "
                                "missing custom splash, it serves the collage its post-scan "
                                "task regenerates")

    generated = data_dir / "splashscreen.png"
    for finding in findings:
        print(f"  FAIL  {finding}")
    if generated.is_file():
        print(f"  note  {generated} is Jellyfin's own generated collage "
              "(SplashscreenPostScanTask rewrites it after every scan); the "
              "SplashscreenLocation above is what overrides it")
    if findings:
        print(f"\nFAIL — {len(findings)} problem(s): the login screen is not showing the "
              "deployment's splash. Apply it with: python3 scripts/jellyfin-splash.py --apply")
        return 1
    print(f"  ok    {installed.name} is installed "
          f"({expected_size[0]}×{expected_size[1]}) and "
          f"{branding['path'].name} points at {expected}")
    return 0


def apply(args) -> int:
    size = RESOLUTION_SIZE[args.scale]
    installed_name = asset_name(args.variant, args.scale)
    asset = Path(args.asset)
    if not asset.is_file():
        if args.render:
            print(f"{asset} is missing — rendering it ({args.variant}, "
                  f"{size[0]}×{size[1]})")
            render(args.variant, asset, args.font_serif, args.font_sans, size=size)
        else:
            raise CannotRun(f"{asset} is missing; run --render first")
    width, height = png_dimensions(asset)
    if (width, height) != size:
        raise CannotRun(f"{asset} is {width}×{height}, expected "
                        f"{size[0]}×{size[1]}")

    data_dir = Path(args.data_path)
    if not data_dir.is_dir():
        raise CannotRun(f"{data_dir} does not exist — is this the media host's appdata? "
                        "(set APPDATA or --data-path)")
    installed = data_dir / installed_name
    shutil.copyfile(asset, installed)
    print(f"installed    {installed} (from {asset.name})")

    # The file Jellyfin regenerates. Written too, because it is what a client
    # shows when the custom location is not honoured — the location is the fix,
    # this is the belt.
    generated = data_dir / "splashscreen.png"
    if generated.is_file():
        shutil.copyfile(asset, generated)
        print(f"replaced     {generated} (Jellyfin's generated collage)")

    location = f"{args.container_data_path.rstrip('/')}/{installed_name}"
    path = write_branding(Path(args.config_dir), location)
    print(f"branding     {path} → SplashscreenEnabled=true, SplashscreenLocation={location}")
    if not args.restart:
        print("restart Jellyfin for it to load the branding (--restart does it over the API)")
    return 0


def restart_jellyfin(args) -> bool:
    import urllib.request

    key = args.api_key
    if not key:
        key_file = Path(args.api_key_file)
        try:
            key = key_file.read_text(encoding="utf-8").strip()
        except OSError:
            print(f"  note  no API key ({key_file} unreadable, --api-key unset) — restart "
                  "Jellyfin yourself")
            return False
    request = urllib.request.Request(f"{args.jellyfin_url.rstrip('/')}/System/Restart", method="POST")
    for header, value in (("Authorization", f'MediaBrowser Token="{key}"'),
                          ("Content-Type", "application/json")):
        request.add_header(header, value)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            print(f"restarted    Jellyfin answered HTTP {response.status} for /System/Restart")
        return True
    except Exception as error:  # noqa: BLE001 - a restart is best effort
        print(f"  note  restart request failed ({error}) — restart Jellyfin yourself")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report only (the default)")
    parser.add_argument("--apply", action="store_true", help="install the splash and point branding at it")
    parser.add_argument("--render", action="store_true", help="re-render the 4K PNG (needs Pillow)")
    parser.add_argument("--svg", action="store_true", help="rewrite the design-of-record SVG")
    parser.add_argument("--variant", choices=sorted(DESIGN), default="dark")
    parser.add_argument("--scale", type=int, choices=sorted(RESOLUTION_SIZE, reverse=True),
                        default=SCALE,
                        help="2 = 4K (default), 1 = 1080p for a client that caps "
                             "the size of a splash image")
    parser.add_argument("--asset", default="",
                        help="the PNG to install (default assets/monarch-splash.png)")
    parser.add_argument("--appdata", default=os.environ.get("APPDATA", "/docker/appdata"))
    parser.add_argument("--data-path", default="", help="Jellyfin's data dir (default <appdata>/jellyfin/data/data)")
    parser.add_argument("--config-dir", default="", help="Jellyfin's config dir (default <appdata>/jellyfin)")
    parser.add_argument("--container-data-path", default=DEFAULT_CONTAINER_DATA,
                        help="the data dir as *Jellyfin* sees it, for SplashscreenLocation")
    parser.add_argument("--container-config-path", default=DEFAULT_CONTAINER_CONFIG,
                        help="the appdata mount as *Jellyfin* sees it (for mapping the "
                             "recorded SplashscreenLocation back to a host path)")
    parser.add_argument("--font-serif", default="")
    parser.add_argument("--font-sans", default="")
    parser.add_argument("--restart", action="store_true", help="POST /System/Restart after applying")
    parser.add_argument("--jellyfin-url",
                        default=os.environ.get("JELLYFIN_HOST_URL", "http://127.0.0.1:8097"))
    parser.add_argument("--api-key", default=os.environ.get("JELLYFIN_API_KEY", ""))
    parser.add_argument("--api-key-file", default="", help="default <appdata>/init/jellyfin-api-key.txt")
    args = parser.parse_args(argv)

    appdata = Path(args.appdata)
    # `--variant light` used to install the *dark* 4K asset when no --asset was
    # given (the default was the module-level SPLASH_ASSET), because the variant
    # only ever moved the render target. The default now follows the variant and
    # the scale, which is also what --check judges against.
    args.asset_given = bool(args.asset)
    asset_target = (Path(args.asset) if args.asset
                    else ASSET_DIR / asset_name(args.variant, args.scale))
    args.asset = str(asset_target)
    args.data_path = args.data_path or str(appdata / "jellyfin" / "data" / "data")
    args.config_dir = args.config_dir or str(appdata / "jellyfin")
    args.api_key_file = args.api_key_file or str(appdata / "init" / "jellyfin-api-key.txt")

    try:
        if args.svg and not args.apply:
            target = ASSET_DIR / DESIGN[args.variant]["svg"]
            target.write_text(svg(args.variant), encoding="utf-8")
            print(f"svg          {target}")
            return 0
        if args.render and not args.apply:
            render(args.variant, asset_target, args.font_serif, args.font_sans,
                   size=RESOLUTION_SIZE[args.scale])
            print(f"rendered     {asset_target} ({RESOLUTION_SIZE[args.scale][0]}×"
                  f"{RESOLUTION_SIZE[args.scale][1]})")
            return 0
        if args.apply:
            code = apply(args)
            if args.restart:
                restart_jellyfin(args)
            return code
        return check(args)
    except CannotRun as error:
        print(f"jellyfin-splash: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
