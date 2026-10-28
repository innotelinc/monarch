#!/usr/bin/env python3
"""jellyfin-splash.py — Monarch's splash screen, reproduced from the repo.

WHY THIS EXISTS
---------------
The mission claims a branded splash ("the obsidian/champagne splash, source SVG
in `assets/`, installed 1920×1080 PNG"), and nothing in this repo ever made it:
no script rendered the SVG and no script put the result where Jellyfin reads it.
What is actually on disk on the media host is Jellyfin's **own generated
collage** — `SplashscreenPostScanTask` builds it from up to 30 random posters and
30 thumbnails after *every* library scan and writes it to
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
  version of this script recorded `/config/data/monarch-splash.png` while
  installing to `/config/data/data/monarch-splash.png` (this image's data dir is
  `{config}/data/data`), and the measurement that caught it was putting a
  *different* image in the collage's place and watching which one came back from
  `/Branding/Splashscreen`. `--check` now maps the recorded path back through the
  appdata mount and fails when no file is there.

So `--apply` does two things, and both are needed: it puts the rendered image in
the data directory under a name Jellyfin never writes, and it records that path
in `branding.xml` as the custom splash. It also leaves a copy at
`{DataPath}/splashscreen.png`, because that is the file the collage overwrites
and the file a client shows when the location is not honoured.

The image itself is **rendered from this repo** (`assets/monarch-splash.svg` is
the design; the geometry and palette below are its, and
`scripts/tests/test_jellyfin_splash.py` fails when the two drift). Rendering
needs Pillow, which is why the rendered PNG is committed beside the SVG:
`--check` and `--apply` are stdlib-only and work on a host that has no rasterizer
at all (neither `.30` nor this repo's CI has one). `--render` regenerates it.

USAGE
-----
    python3 scripts/jellyfin-splash.py --check        # report only (default)
    python3 scripts/jellyfin-splash.py --apply        # install + point branding at it
    python3 scripts/jellyfin-splash.py --render       # re-render assets/*.png (needs Pillow)
    python3 scripts/jellyfin-splash.py --apply --variant light

Runs on a host that mounts the media host's appdata (`APPDATA`, default
`/docker/appdata`), or inside a container that has it. Paths:

    asset           assets/monarch-splash.png           (this repo)
    config          $APPDATA/jellyfin/branding.xml      (Jellyfin's /config)
    installed       $APPDATA/jellyfin/data/data/monarch-splash.png
                    recorded in branding.xml as /config/data/data/monarch-splash.png
                    (the *container* path Jellyfin resolves, not the host one)

Exit codes: 0 = the branded splash is installed and wired; 1 = it is not (a
finding is printed); 2 = cannot run (no asset and no Pillow, or no appdata).
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = REPO_ROOT / "assets"

# The design. Every value here is the SVG's (`assets/monarch-splash.svg` for the
# dark variant, `-light` for the other) — the test suite reads the SVG back and
# fails when these stop matching, so they are not free parameters.
DESIGN = {
    "dark": {
        "asset": "monarch-splash.png",
        "bg": ((0.0, "#08090b"), (0.52, "#111316"), (1.0, "#070809")),
        "glow": {
            "colour": "#d8b56d", "alpha": 0.12, "fade": "#8e6b36", "fade_alpha": 0.035,
            "cx": 960, "cy": 430, "rx": 570, "ry": 450,
        },
        "rings": {"colour": "#caa35d", "alpha": 0.18, "cy": 420, "radii": (290, 370)},
        "rules": {"colour": "#caa35d", "alpha": 0.5, "rows": (124, 956), "spans": ((160, 540), (1380, 1760))},
        "dots": {"colour": "#d8b56d", "r": 3, "points": ((560, 124), (1360, 124), (560, 956), (1360, 956))},
        "monogram": {
            "cx": 960, "cy": 365,
            "halo": {"colour": "#d8b56d", "alpha": 0.07, "r": 190, "blur": 26},
            "ring_outer": {"gradient": True, "width": 2, "alpha": 0.9, "r": 142},
            "ring_inner": {"colour": "#caa35d", "width": 1, "alpha": 0.3, "r": 129},
            "bars": {"colour": "#f2d99a", "width": 7, "from_y": 92, "to_y": -67, "xs": (-12, 12)},
            # Each wing is the left-hand cubic; the right one is its mirror, which
            # is how the SVG spells it out (M-8 -58… / M8 -58… are mirror images).
            "wings": [
                {"width": 9, "gradient": True, "alpha": 1.0,
                 "path": ((-8, -58), (-56, -114), (-87, -122), (-110, -99)),
                 "mirror": True},
                {"width": 8, "colour": "#caa35d", "alpha": 1.0,
                 "path": ((-18, 15), (-89, 65), (-111, 93), (-83, 106)), "mirror": True},
            ],
            "antennae": {"colour": "#f2d99a", "width": 3,
                         "path": ((-6, -75), (-28, -111), (-39, -126), (-59, -139))},
            "antenna_dots": {"colour": "#f2d99a", "r": 4, "points": ((-61, -141), (61, -141))},
        },
        "text": [
            {"string": "MONARCH", "y": 690, "size": 108, "tracking": 25,
             "colour": "#f5f0e4", "font": "serif"},
            {"string": "MEDIA PLATFORM", "y": 765, "size": 22, "tracking": 12,
             "colour": "#d8b56d", "font": "sans-semibold"},
            {"string": "YOUR LIBRARY · BEAUTIFULLY ORGANIZED", "y": 872, "size": 20,
             "tracking": 6, "colour": "#aaa69d", "font": "sans"},
        ],
        "text_rule": {"y": 816, "x": (780, 1140), "colour": "#caa35d", "width": 1},
    },
    "light": {
        "asset": "monarch-splash-light.png",
        "bg": ((0.0, "#faf7ef"), (1.0, "#ebe3d2")),
        "glow": {"colour": "#caa35d", "alpha": 0.08, "fade": "#caa35d", "fade_alpha": 0.0,
                 "cx": 960, "cy": 410, "rx": 330, "ry": 330},
        "rings": {"colour": "#8e6b36", "alpha": 0.42, "cy": 410, "radii": (290, 370)},
        "rules": {"colour": "#8e6b36", "alpha": 0.0, "rows": (), "spans": ()},
        "dots": {"colour": "#8e6b36", "r": 0, "points": ()},
        "monogram": {
            "cx": 960, "cy": 365,
            "halo": {"colour": "#caa35d", "alpha": 0.0, "r": 190, "blur": 26},
            "ring_outer": {"gradient": True, "width": 2, "alpha": 1.0, "r": 142},
            "ring_inner": {"colour": "#8e6b36", "width": 0, "alpha": 0.0, "r": 129},
            "bars": {"colour": "#8e6b36", "width": 7, "from_y": 88, "to_y": -70, "xs": (-10, 10)},
            "wings": [
                {"width": 9, "gradient": True, "alpha": 1.0,
                 "path": ((-8, -58), (-58, -115), (-88, -122), (-112, -98)), "mirror": True},
                {"width": 9, "gradient": True, "alpha": 1.0,
                 "path": ((-18, 15), (-90, 65), (-110, 94), (-82, 106)), "mirror": True},
            ],
            "antennae": {"colour": "#8e6b36", "width": 3,
                         "path": ((-7, -75), (-30, -112), (-40, -126), (-60, -139))},
            "antenna_dots": {"colour": "#8e6b36", "r": 4, "points": ((-61, -141), (61, -141))},
        },
        "text": [
            {"string": "MONARCH", "y": 690, "size": 108, "tracking": 25,
             "colour": "#25231f", "font": "serif"},
            {"string": "MEDIA PLATFORM", "y": 765, "size": 22, "tracking": 12,
             "colour": "#8e6b36", "font": "sans-semibold"},
            {"string": "YOUR LIBRARY · BEAUTIFULLY ORGANIZED", "y": 872, "size": 20,
             "tracking": 6, "colour": "#70695f", "font": "sans"},
        ],
        "text_rule": {"y": 816, "x": (780, 1140), "colour": "#caa35d", "width": 1},
    },
}

# The gold used for the monogram's gradient strokes (both variants name their own
# stops in their SVG; the dark one is shared with the wordmark accents).
GOLD = {
    "dark": ((0.0, "#f2d99a"), (0.48, "#caa35d"), (1.0, "#8e6b36")),
    "light": ((0.0, "#8e6b36"), (0.5, "#caa35d"), (1.0, "#6e5228")),
}

WIDTH, HEIGHT = 1920, 1080

# Georgia is not on a Linux host and DejaVu is; the SVG names a family chain, so
# the first available of each kind is the faithful-enough substitute. Override
# with --font-serif/--font-sans when a host has better ones.
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
INSTALLED_NAME = "monarch-splash.png"
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


# ── the renderer (Pillow only; --check and --apply never import it) ─────────

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


def _cubic(points, steps: int = 40):
    """Sample one cubic `<path>` segment into (x, y) points."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = points
    out = []
    for index in range(steps + 1):
        t = index / steps
        u = 1 - t
        out.append((
            u ** 3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3,
            u ** 3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3,
        ))
    return out


def _draw_polyline(draw, points, colour, width: int) -> None:
    if width <= 0 or len(points) < 2:
        return
    draw.line([(round(x), round(y)) for x, y in points], fill=colour, width=width, joint="curve")


def render(variant: str, out_path: Path, font_serif: str = "", font_sans: str = "") -> Path:
    """Render the design to `out_path`. Needs Pillow (and a font) on this host."""
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
    except ImportError as error:  # pragma: no cover - host without Pillow
        raise CannotRun(
            f"Pillow is required to render the splash ({error}). Either install "
            "python3-pil, or use the committed asset: --check/--apply need no "
            "rasterizer.") from None

    design = DESIGN[variant]
    gold = GOLD[variant]
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 255))
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Background: the SVG's 0,0 -> 1,1 object bounding box gradient. Built small
    # and scaled up, because per-pixel work over 2M pixels in Python is not free
    # and a smooth ramp survives bilinear scaling exactly.
    small = Image.new("RGB", (192, 108))
    for y in range(108):
        for x in range(192):
            t = (x / 191 + y / 107) / 2
            small.putpixel((x, y), tuple(int(c) for c in _lerp_stops(design["bg"], t)))
    image.paste(small.resize((WIDTH, HEIGHT), Image.BILINEAR), (0, 0))

    glow = design["glow"]
    if glow["alpha"] > 0:
        layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        halo = ImageDraw.Draw(layer)
        r, g, b = _rgb(glow["colour"])
        fade_r, fade_g, fade_b = _rgb(glow["fade"])
        steps = 60
        for index in range(steps, 0, -1):
            k = index / steps
            alpha = int(255 * (glow["alpha"] * (1 - k) + glow["fade_alpha"] * k))
            if alpha <= 0:
                continue
            colour = (int(r + (fade_r - r) * k), int(g + (fade_g - g) * k),
                      int(b + (fade_b - b) * k), alpha)
            halo.ellipse(
                (glow["cx"] - glow["rx"] * k, glow["cy"] - glow["ry"] * k,
                 glow["cx"] + glow["rx"] * k, glow["cy"] + glow["ry"] * k),
                fill=colour)
        overlay = Image.alpha_composite(overlay, layer)
        draw = ImageDraw.Draw(overlay)

    rings = design["rings"]
    if rings["alpha"] > 0:
        ring_colour = (*_rgb(rings["colour"]), int(255 * rings["alpha"]))
        for radius in rings["radii"]:
            draw.ellipse((design["monogram"]["cx"] - radius, rings["cy"] - radius,
                          design["monogram"]["cx"] + radius, rings["cy"] + radius),
                         outline=ring_colour, width=1)

    rules = design["rules"]
    if rules["alpha"] > 0 and rules["rows"]:
        rule_colour = (*_rgb(rules["colour"]), int(255 * rules["alpha"]))
        for row in rules["rows"]:
            for start, end in rules["spans"]:
                draw.line([(start, row), (end, row)], fill=rule_colour, width=1)

    dots = design["dots"]
    for cx, cy in dots["points"] if dots["r"] else ():
        draw.ellipse((cx - dots["r"], cy - dots["r"], cx + dots["r"], cy + dots["r"]),
                     fill=(*_rgb(dots["colour"]), 255))

    monogram = design["monogram"]
    cx, cy = monogram["cx"], monogram["cy"]

    halo = monogram["halo"]
    if halo["alpha"] > 0:
        layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse(
            (cx - halo["r"], cy - halo["r"], cx + halo["r"], cy + halo["r"]),
            fill=(*_rgb(halo["colour"]), int(255 * halo["alpha"])))
        overlay = Image.alpha_composite(overlay, layer.filter(ImageFilter.GaussianBlur(halo["blur"])))
        draw = ImageDraw.Draw(overlay)

    for key in ("ring_outer", "ring_inner"):
        ring = monogram[key]
        if ring["alpha"] <= 0 or ring["r"] <= 0:
            continue
        ring_colour = (*_rgb("#caa35d" if ring.get("gradient") else ring["colour"]),
                       int(255 * ring["alpha"]))
        draw.ellipse((cx - ring["r"], cy - ring["r"], cx + ring["r"], cy + ring["r"]),
                     outline=ring_colour, width=ring["width"] or 1)

    bars = monogram["bars"]
    for offset in bars["xs"]:
        draw.line([(cx + offset, cy + bars["from_y"]), (cx + offset, cy + bars["to_y"])],
                  fill=(*_rgb(bars["colour"]), 255), width=bars["width"])

    for wing in monogram["wings"]:
        points = _cubic(wing["path"])
        for mirrored in ((1,) if not wing["mirror"] else (1, -1)):
            placed = [(cx + mirrored * x, cy + y) for x, y in points]
            if wing.get("gradient"):
                # The stroke follows the gold gradient along the path's own
                # progress, which is what the SVG's gradient stroke does across
                # the shape's bounding box (close enough at these widths).
                for index in range(len(placed) - 1):
                    t = index / (len(placed) - 1)
                    colour = tuple(int(c) for c in _lerp_stops(gold, t))
                    _draw_polyline(draw, placed[index:index + 2],
                                   (*colour, int(255 * wing["alpha"])), wing["width"])
            else:
                _draw_polyline(draw, placed,
                               (*_rgb(wing["colour"]), int(255 * wing["alpha"])), wing["width"])

    antennae = monogram["antennae"]
    for mirrored in (1, -1):
        _draw_polyline(draw, [(cx + mirrored * x, cy + y) for x, y in _cubic(antennae["path"])],
                       (*_rgb(antennae["colour"]), 255), antennae["width"])

    adots = monogram["antenna_dots"]
    for x, y in adots["points"]:
        draw.ellipse((cx + x - adots["r"], cy + y - adots["r"],
                      cx + x + adots["r"], cy + y + adots["r"]),
                     fill=(*_rgb(adots["colour"]), 255))

    image = Image.alpha_composite(image, overlay)

    # Text last, over everything, each glyph placed with the SVG's letter-spacing.
    text_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_layer)
    for spec in design["text"]:
        if spec["font"] == "serif":
            path = font_serif or next((p for p in FONTS["serif"] if os.path.exists(p)), "")
        else:
            path = font_sans or next((p for p in FONTS[spec["font"]] if os.path.exists(p)), "")
        if not path or not os.path.exists(path):
            raise CannotRun(
                f"no {spec['font']} font found on this host (looked in "
                f"{FONTS[spec['font']]}); pass --font-serif/--font-sans")
        font = ImageFont.truetype(path, spec["size"])
        advances = [text_draw.textlength(ch, font=font) for ch in spec["string"]]
        total = sum(advances) + spec["tracking"] * (len(spec["string"]) - 1)
        x = (WIDTH - total) / 2
        for ch, advance in zip(spec["string"], advances):
            text_draw.text((x, spec["y"]), ch, font=font, fill=(*_rgb(spec["colour"]), 255),
                           anchor="ls")
            x += advance + spec["tracking"]
    image = Image.alpha_composite(image, text_layer)

    rule = design["text_rule"]
    if rule["width"] > 0:
        ImageDraw.Draw(image).line(
            [(rule["x"][0], rule["y"]), (rule["x"][1], rule["y"])],
            fill=(*_rgb(rule["colour"]), 255), width=rule["width"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(out_path, "PNG", optimize=True)
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
            if (width, height) != (WIDTH, HEIGHT):
                findings.append(f"{asset} is {width}×{height}, expected {WIDTH}×{HEIGHT}")

    installed = data_dir / INSTALLED_NAME
    if not installed.is_file():
        findings.append(f"{installed} is missing — the branded splash is not installed")
    elif asset.is_file() and not same_bytes(installed, asset):
        findings.append(f"{installed} is not {asset.name} — it has been replaced")

    branding = read_branding(config_dir)
    location = branding["location"]
    expected = f"{args.container_data_path.rstrip('/')}/{INSTALLED_NAME}"
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
        print(f"\nFAIL — {len(findings)} problem(s): the login screen is not showing Monarch's "
              "splash. Apply it with: python3 scripts/jellyfin-splash.py --apply")
        return 1
    print(f"  ok    {installed.name} is installed ({WIDTH}×{HEIGHT}) and "
          f"{branding['path'].name} points at {expected}")
    return 0


def apply(args) -> int:
    asset = Path(args.asset)
    if not asset.is_file():
        if args.render:
            print(f"{asset} is missing — rendering it ({args.variant})")
            render(args.variant, asset, args.font_serif, args.font_sans)
        else:
            raise CannotRun(f"{asset} is missing; run --render first")
    width, height = png_dimensions(asset)
    if (width, height) != (WIDTH, HEIGHT):
        raise CannotRun(f"{asset} is {width}×{height}, expected {WIDTH}×{HEIGHT}")

    data_dir = Path(args.data_path)
    if not data_dir.is_dir():
        raise CannotRun(f"{data_dir} does not exist — is this the media host's appdata? "
                        "(set APPDATA or --data-path)")
    installed = data_dir / INSTALLED_NAME
    shutil.copyfile(asset, installed)
    print(f"installed    {installed} (from {asset.name})")

    # The file Jellyfin regenerates. Written too, because it is what a client
    # shows when the custom location is not honoured — the location is the fix,
    # this is the belt.
    generated = data_dir / "splashscreen.png"
    if generated.is_file():
        shutil.copyfile(asset, generated)
        print(f"replaced     {generated} (Jellyfin's generated collage)")

    location = f"{args.container_data_path.rstrip('/')}/{INSTALLED_NAME}"
    path = write_branding(Path(args.config_dir), location)
    print(f"branding     {path} → SplashscreenEnabled=true, SplashscreenLocation={location}")
    if not args.restart:
        print("restart Jellyfin for it to load the branding (--restart does it over the API)")
    return 0


def restart_jellyfin(args) -> bool:
    import json
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
    parser.add_argument("--render", action="store_true", help="re-render the asset from the design (needs Pillow)")
    parser.add_argument("--variant", choices=sorted(DESIGN), default="dark")
    parser.add_argument("--asset", default="", help="the rendered PNG (default assets/<variant>.png)")
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
    parser.add_argument("--jellyfin-url", default=os.environ.get("JELLYFIN_URL", "http://127.0.0.1:8097"))
    parser.add_argument("--api-key", default=os.environ.get("JELLYFIN_API_KEY", ""))
    parser.add_argument("--api-key-file", default="", help="default <appdata>/init/jellyfin-api-key.txt")
    args = parser.parse_args(argv)

    appdata = Path(args.appdata)
    args.asset = args.asset or str(ASSET_DIR / DESIGN[args.variant]["asset"])
    args.data_path = args.data_path or str(appdata / "jellyfin" / "data" / "data")
    args.config_dir = args.config_dir or str(appdata / "jellyfin")
    args.api_key_file = args.api_key_file or str(appdata / "init" / "jellyfin-api-key.txt")

    try:
        if args.render and not args.apply:
            render(args.variant, Path(args.asset), args.font_serif, args.font_sans)
            print(f"rendered     {args.asset}")
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
