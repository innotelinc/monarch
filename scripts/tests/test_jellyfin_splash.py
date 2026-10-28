#!/usr/bin/env python3
"""Unit tests for jellyfin-splash.py — Monarch's own splash screen.

WHY THIS EXISTS
---------------
The splash a Jellyfin client shows is normally *generated*: the server builds a
collage from up to 30 posters and 30 thumbnails after every library scan and
writes it to `{DataPath}/splashscreen.png` (1920×1080, ~4 MB of poster wall). The
only way a deployment shows its own image is `SplashscreenLocation` in the
`branding` configuration store — which the API deliberately cannot set, so the
file has to be written, and that is what `jellyfin-splash.py` does.

Three things are pinned here, because each of them is wrong quietly:

  * **The renderer and the design agree.** `assets/monarch-splash.svg` is the
    design and `DESIGN` in the script is a second copy of it (Pillow cannot
    rasterize SVG, and the hosts that install the splash have no rasterizer at
    all). Two copies drift, so the SVG is read back and every number and colour
    the renderer draws with must appear in it. Change the art, update the
    constants, or this fails.
  * **The installed file is the asset, and the recorded path is Jellyfin's.** The
    path written into `branding.xml` is resolved *inside the container*
    (`/config/data/data/...`), not on the host — recording the host path would point
    the server at a file it cannot see while the check reported success. And
    `--check` must fail when what is installed is not the asset, which is exactly
    the state the media host was found in.
  * **`--apply` does not lose the rest of the branding.** `branding.xml` also
    carries `LoginDisclaimer` and `CustomCss`; a wholesale rewrite drops them,
    and an API-side branding write is what erases `SplashscreenLocation` in the
    first place (jellyfin/jellyfin#13744). The file is edited, not replaced.

Everything except the renderer's own tests runs without Pillow, because CI and
the media host's peers do not have it: the committed PNG is what gets installed.

The module under test has a hyphen in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import struct
import sys
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "jellyfin-splash.py"
SVG = {"dark": REPO_ROOT / "assets" / "monarch-splash.svg",
       "light": REPO_ROOT / "assets" / "monarch-splash-light.svg"}

try:
    import PIL  # noqa: F401
    HAVE_PILLOW = True
except ImportError:  # pragma: no cover - CI has no Pillow
    HAVE_PILLOW = False


def _load():
    spec = importlib.util.spec_from_file_location("jellyfin_splash", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


splash = _load()


def run(*args: str) -> tuple[int, str, str]:
    """Call the script's main() and capture (exit code, stdout, stderr)."""
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = splash.main(list(args))
    return code, out.getvalue(), err.getvalue()


def appdata(tmp: Path, *, branding: str | None = None, collage: bool = True) -> Path:
    """A scratch appdata that looks like the media host's."""
    data = tmp / "jellyfin" / "data" / "data"
    data.mkdir(parents=True)
    if collage:
        (data / "splashscreen.png").write_bytes(
            # 1x1 PNG, standing in for the multi-megabyte collage Jellyfin writes.
            bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                          "0000000d4944415478da63f8ffff3f0300050001a5f645400000000049454e44ae426082"))
    if branding is not None:
        (tmp / "jellyfin" / "branding.xml").write_text(branding, encoding="utf-8")
    return tmp


class DesignMatchesTheSvg(unittest.TestCase):
    """The renderer is a second copy of the art; this is what stops the drift."""

    def test_dark_design_is_the_dark_svg(self):
        svg = SVG["dark"].read_text(encoding="utf-8")
        design = splash.DESIGN["dark"]
        # Background: the SVG's 0,0 → 1,1 gradient and its three stops.
        self.assertIn('width="1920" height="1080"', svg)
        self.assertIn("#08090b", svg)
        self.assertIn("#111316", svg)
        self.assertIn("#070809", svg)
        self.assertEqual([c for _, c in design["bg"]], ["#08090b", "#111316", "#070809"])
        # The radial wash and the two hairline circles under the monogram.
        self.assertIn('cx="960" cy="430" rx="570" ry="450"', svg)
        glow = design["glow"]
        self.assertEqual((glow["cx"], glow["cy"], glow["rx"], glow["ry"]), (960, 430, 570, 450))
        self.assertIn('<circle cx="960" cy="420" r="290"', svg)
        self.assertIn('<circle cx="960" cy="420" r="370"', svg)
        self.assertEqual(tuple(design["rings"]["radii"]), (290, 370))
        # Corner rules and the four dots that end them.
        for literal in ("M160 124H540", "M1380 124H1760", "M160 956H540", "M1380 956H1760"):
            self.assertIn(literal, svg)
        self.assertIn('<circle cx="560" cy="124" r="3"/>', svg)
        self.assertIn('<circle cx="1360" cy="956" r="3"/>', svg)
        self.assertEqual(set(design["dots"]["points"]), {(560, 124), (1360, 124), (560, 956), (1360, 956)})
        # The monogram: translate, the two rings, the two bars, both wing cubics,
        # the antennae and their tips.
        self.assertIn('transform="translate(960 365)"', svg)
        self.assertEqual((design["monogram"]["cx"], design["monogram"]["cy"]), (960, 365))
        for literal in ('<circle r="190"', '<circle r="142"', '<circle r="129"'):
            self.assertIn(literal, svg)
        self.assertEqual(design["monogram"]["halo"]["r"], 190)
        self.assertEqual(design["monogram"]["ring_outer"]["r"], 142)
        self.assertEqual(design["monogram"]["ring_inner"]["r"], 129)
        self.assertIn("M-12 92V-67M12 92V-67", svg)
        self.assertEqual(design["monogram"]["bars"]["xs"], (-12, 12))
        self.assertEqual((design["monogram"]["bars"]["from_y"], design["monogram"]["bars"]["to_y"]), (92, -67))
        self.assertIn("M-8-58C-56-114-87-122-110-99C-136-74-108-28-19 14", svg)
        self.assertIn("M-18 15C-89 65-111 93-83 106C-52 121-24 87-7 42", svg)
        self.assertEqual(design["monogram"]["wings"][0]["path"],
                         ((-8, -58), (-56, -114), (-87, -122), (-110, -99)))
        self.assertEqual(design["monogram"]["wings"][1]["path"],
                         ((-18, 15), (-89, 65), (-111, 93), (-83, 106)))
        self.assertIn("M-6-75C-28-111-39-126-59-139", svg)
        self.assertEqual(design["monogram"]["antennae"]["path"],
                         ((-6, -75), (-28, -111), (-39, -126), (-59, -139)))
        self.assertIn('cx="-61" cy="-141" r="4"', svg)
        self.assertIn('cx="61" cy="-141" r="4"', svg)
        # The three text runs, with the tracking the SVG sets.
        for y, size, tracking in ((690, 108, 25), (765, 22, 12), (872, 20, 6)):
            self.assertIn(f'y="{y}"', svg)
            self.assertIn(f'font-size="{size}"', svg)
            self.assertIn(f'letter-spacing="{tracking}"', svg)
        strings = [t["string"] for t in design["text"]]
        self.assertEqual(strings, ["MONARCH", "MEDIA PLATFORM", "YOUR LIBRARY · BEAUTIFULLY ORGANIZED"])
        for text in design["text"]:
            self.assertEqual((text["y"], text["size"], text["tracking"]),
                             {"MONARCH": (690, 108, 25), "MEDIA PLATFORM": (765, 22, 12),
                              "YOUR LIBRARY · BEAUTIFULLY ORGANIZED": (872, 20, 6)}[text["string"]])
        self.assertIn("M780 816H1140", svg)
        self.assertEqual(design["text_rule"]["y"], 816)
        self.assertEqual(tuple(design["text_rule"]["x"]), (780, 1140))
        # Every colour the renderer paints with is one the design names.
        palette = set()
        for key in ("rings", "dots", "text_rule"):
            palette.add(design[key]["colour"])
        palette.add(design["glow"]["colour"])
        palette.add(design["glow"]["fade"])
        palette.add(design["monogram"]["halo"]["colour"])
        palette.add(design["monogram"]["ring_inner"]["colour"])
        palette.add(design["monogram"]["bars"]["colour"])
        palette.add(design["monogram"]["antennae"]["colour"])
        palette.add(design["monogram"]["antenna_dots"]["colour"])
        palette.update(text["colour"] for text in design["text"])
        palette.update(colour for _, colour in splash.GOLD["dark"])
        for colour in palette:
            self.assertIn(colour, svg.lower(), f"{colour} is drawn but not in the design")

    def test_light_design_is_the_light_svg(self):
        svg = SVG["light"].read_text(encoding="utf-8")
        design = splash.DESIGN["light"]
        self.assertIn("#faf7ef", svg)
        self.assertIn("#ebe3d2", svg)
        self.assertEqual([c for _, c in design["bg"]], ["#faf7ef", "#ebe3d2"])
        self.assertIn('<circle cx="960" cy="410" r="330"', svg)
        self.assertEqual((design["glow"]["cx"], design["glow"]["cy"], design["glow"]["rx"]), (960, 410, 330))
        self.assertIn('<circle cx="960" cy="410" r="290"', svg)
        self.assertIn('<circle cx="960" cy="410" r="370"', svg)
        self.assertEqual(tuple(design["rings"]["radii"]), (290, 370))
        self.assertIn("M-10 88V-70M10 88V-70", svg)
        self.assertEqual((design["monogram"]["bars"]["xs"],
                          design["monogram"]["bars"]["from_y"],
                          design["monogram"]["bars"]["to_y"]), ((-10, 10), 88, -70))
        self.assertIn("M-8-58C-58-115-88-122-112-98", svg)
        self.assertIn("M-18 15C-90 65-110 94-82 106", svg)
        self.assertIn("M-7-75C-30-112-40-126-60-139", svg)
        self.assertIn('y="690"', svg)
        self.assertIn('font-size="108"', svg)
        for colour in (t["colour"] for t in design["text"]):
            self.assertIn(colour, svg)
        self.assertEqual([t["colour"] for t in design["text"]], ["#25231f", "#8e6b36", "#70695f"])
        for colour in ("#8e6b36", "#caa35d", "#6e5228"):
            self.assertIn(colour, svg)
        for _, colour in splash.GOLD["light"]:
            self.assertIn(colour, svg)


class CommittedAsset(unittest.TestCase):
    """The asset is what gets installed, so its shape is asserted, not assumed."""

    def test_dark_asset_is_a_1080p_rgb_png(self):
        asset = REPO_ROOT / "assets" / "monarch-splash.png"
        self.assertTrue(asset.is_file(), "assets/monarch-splash.png is not committed")
        self.assertEqual(splash.png_dimensions(asset), (1920, 1080))
        colour_type = asset.read_bytes()[25]
        self.assertIn(colour_type, (2, 6), "expected a truecolour PNG")

    def test_asset_is_a_flat_design_not_a_photo_collage(self):
        # The splash this replaces is Jellyfin's generated collage: 1920×1080 and
        # 4.1 MB, because it is photographs. A flat design with gradients is
        # fractions of that, so an accidental re-import of a collage is caught.
        asset = REPO_ROOT / "assets" / "monarch-splash.png"
        self.assertLess(asset.stat().st_size, 500_000,
                        "the asset is as large as a photo collage — is this Jellyfin's "
                        "generated splash rather than the rendered design?")

    def test_committed_png_matches_a_fresh_render(self):
        if not HAVE_PILLOW:
            self.skipTest("Pillow is not installed; only the committed asset is judged here")
        with TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "fresh.png"
            splash.render("dark", fresh)
            self.assertEqual(splash.png_dimensions(fresh), (1920, 1080))
            self.assertTrue(splash.same_bytes(fresh, REPO_ROOT / "assets" / "monarch-splash.png"),
                            "the committed asset is not what this design renders — "
                            "re-run --render and commit the result")

    def test_render_is_deterministic(self):
        if not HAVE_PILLOW:
            self.skipTest("Pillow is not installed")
        with TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a.png", Path(tmp) / "b.png"
            splash.render("dark", first)
            splash.render("dark", second)
            self.assertTrue(splash.same_bytes(first, second))


class CheckAndApply(unittest.TestCase):

    def test_check_fails_while_the_collage_is_what_is_installed(self):
        with TemporaryDirectory() as tmp:
            root = appdata(Path(tmp))
            code, out, _ = run("--check", "--appdata", str(root))
            self.assertEqual(code, 1)
            self.assertIn("not installed", out)
            # It also says which file is the one Jellyfin regenerates, because
            # that is the file an operator would have replaced by hand.
            self.assertIn("collage", out)

    def test_apply_installs_the_asset_replaces_the_collage_and_points_branding(self):
        with TemporaryDirectory() as tmp:
            root = appdata(Path(tmp))
            code, out, _ = run("--apply", "--appdata", str(root))
            self.assertEqual(code, 0, out)
            data = root / "jellyfin" / "data" / "data"
            asset = REPO_ROOT / "assets" / "monarch-splash.png"
            self.assertTrue(splash.same_bytes(data / "monarch-splash.png", asset))
            self.assertTrue(splash.same_bytes(data / "splashscreen.png", asset),
                            "the file Jellyfin regenerates should carry the design too")
            branding = splash.read_branding(root / "jellyfin")
            self.assertTrue(branding["enabled"])
            self.assertEqual(branding["location"], "/config/data/data/monarch-splash.png")
            # And the check now passes.
            code, out, _ = run("--check", "--appdata", str(root))
            self.assertEqual(code, 0, out)
            self.assertIn("ok", out)

    def test_branding_keeps_the_keys_it_does_not_own(self):
        with TemporaryDirectory() as tmp:
            root = appdata(Path(tmp), branding=(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<BrandingOptions xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
                "  <LoginDisclaimer>Use your Cerulean account</LoginDisclaimer>\n"
                "  <CustomCss>.detailPagePrimaryContainer { font-family: Inter }</CustomCss>\n"
                "  <SplashscreenEnabled>false</SplashscreenEnabled>\n"
                "</BrandingOptions>\n"))
            code, out, _ = run("--apply", "--appdata", str(root))
            self.assertEqual(code, 0, out)
            text = (root / "jellyfin" / "branding.xml").read_text(encoding="utf-8")
            self.assertIn("Use your Cerulean account", text)
            self.assertIn(".detailPagePrimaryContainer", text)
            self.assertIn("<SplashscreenEnabled>true</SplashscreenEnabled>", text)
            self.assertIn("/config/data/data/monarch-splash.png", text)

    def test_check_flags_a_switched_off_splash(self):
        with TemporaryDirectory() as tmp:
            root = appdata(Path(tmp))
            run("--apply", "--appdata", str(root))
            branding_path = root / "jellyfin" / "branding.xml"
            branding_path.write_text(
                branding_path.read_text(encoding="utf-8").replace(
                    "<SplashscreenEnabled>true</SplashscreenEnabled>",
                    "<SplashscreenEnabled>false</SplashscreenEnabled>"), encoding="utf-8")
            code, out, _ = run("--check", "--appdata", str(root))
            self.assertEqual(code, 1)
            self.assertIn("SplashscreenEnabled=false", out)

    def test_check_flags_a_splash_that_is_not_the_asset(self):
        with TemporaryDirectory() as tmp:
            root = appdata(Path(tmp))
            run("--apply", "--appdata", str(root))
            (root / "jellyfin" / "data" / "data" / "monarch-splash.png").write_bytes(b"not the asset")
            code, out, _ = run("--check", "--appdata", str(root))
            self.assertEqual(code, 1)
            self.assertIn("has been replaced", out)

    def test_check_cannot_run_without_an_appdata(self):
        # "the splash is missing" and "this is not the media host" are different
        # facts: the first is a finding an operator fixes, the second means the
        # check never looked, which is exit 2 in this repo's convention.
        with TemporaryDirectory() as tmp:
            code, _, err = run("--check", "--appdata", str(Path(tmp) / "absent"))
            self.assertEqual(code, 2)
            self.assertIn("do not exist", err)

    def test_recorded_location_is_the_container_path_not_the_host_path(self):
        with TemporaryDirectory() as tmp:
            root = appdata(Path(tmp))
            run("--apply", "--appdata", str(root), "--container-data-path", "/data")
            self.assertEqual(splash.read_branding(root / "jellyfin")["location"],
                             "/data/monarch-splash.png")
            self.assertNotIn(str(root), (root / "jellyfin" / "branding.xml").read_text(encoding="utf-8"))


class Renderer(unittest.TestCase):

    def test_render_needs_no_svg_rasterizer(self):
        # The design is expressed in Python precisely so a host with Pillow and no
        # librsvg/inkscape/cairosvg can rebuild it.
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("cairosvg", "rsvg", "inkscape", "wand", "svglib"):
            self.assertNotIn(forbidden, source)

    def test_render_produces_the_design_at_1080p(self):
        if not HAVE_PILLOW:
            self.skipTest("Pillow is not installed")
        from PIL import Image, ImageStat
        with TemporaryDirectory() as tmp:
            dark = Path(tmp) / "dark.png"
            splash.render("dark", dark)
            self.assertEqual(splash.png_dimensions(dark), (1920, 1080))
            mean = ImageStat.Stat(Image.open(dark).convert("RGB")).mean
            self.assertLess(mean[0], 60, "the dark design is an obsidian field, not a bright image")
            light = Path(tmp) / "light.png"
            splash.render("light", light)
            mean = ImageStat.Stat(Image.open(light).convert("RGB")).mean
            self.assertGreater(mean[0], 200, "the light design is a cream field")

    def test_the_png_it_writes_is_a_plain_truecolour_image(self):
        if not HAVE_PILLOW:
            self.skipTest("Pillow is not installed")
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.png"
            splash.render("dark", out)
            header = out.read_bytes()[:26]
            self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", header[16:24]), (1920, 1080))
            self.assertEqual(header[24:26], b"\x08\x02", "8-bit truecolour RGB, no alpha surprises")


if __name__ == "__main__":
    unittest.main()
