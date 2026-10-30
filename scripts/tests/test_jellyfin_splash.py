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

  * **The committed SVG is the renderer's own output.** The design is a list of
    primitives (`build_plan`) with two backends: the Pillow renderer that
    produces the installed 4K PNG, and an SVG writer that produces
    `assets/monarch-splash.svg`. `assets/…svg` is the design of record, so it is
    compared to a fresh `svg()` call byte for byte; if the plan changes without
    the art being regenerated, this fails.
  * **The installed file is the 4K asset, and the recorded path is Jellyfin's.**
    The path written into `branding.xml` is resolved *inside the container*
    (`/config/data/data/...`), not on the host — recording the host path would
    point the server at a file it cannot see while the check reported success.
    And `--check` must fail when what is installed is not the asset, which is
    exactly the state the media host was found in.
  * **`--apply` does not lose the rest of the branding.** `branding.xml` also
    carries `LoginDisclaimer` and `CustomCss`; a wholesale rewrite drops them,
    and an API-side branding write is what erases `SplashscreenLocation` in the
    first place (jellyfin/jellyfin#13744). The file is edited, not replaced.

Everything except the rasterizer's own tests runs without Pillow, because CI and
the media host's peers do not have it: the committed PNG is what gets installed.

The module under test has a hyphen in its filename, so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import struct
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "jellyfin-splash.py"
SVG = {"dark": REPO_ROOT / "assets" / "monarch-splash.svg",
       "light": REPO_ROOT / "assets" / "monarch-splash-light.svg"}
PNG = {"dark": REPO_ROOT / "assets" / "monarch-splash.png",
       "light": REPO_ROOT / "assets" / "monarch-splash-light.png"}

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


class DesignOfRecord(unittest.TestCase):
    """The SVG is generated from the plan; this is what stops the drift."""

    def test_committed_svg_is_drawn_from_the_plan(self):
        for variant in ("dark", "light"):
            committed = SVG[variant].read_text(encoding="utf-8")
            self.assertEqual(committed, splash.svg(variant),
                             f"assets/{SVG[variant].name} is not what build_plan() draws — "
                             "re-run --svg and commit it")

    def test_the_plan_draws_the_media_motif(self):
        # The count of each instrument is the design's contract: a bezel with
        # 71 ticks is a design change, not a rounding difference.
        geo = splash.GEOMETRY
        self.assertEqual(geo["ticks"]["count"], 72)
        self.assertEqual(geo["emblem"]["blades"], 12)
        self.assertEqual(geo["emblem"]["rays"], 48)
        self.assertEqual(len(geo["orbits"]), 2)
        self.assertEqual(geo["stars"], 170)
        self.assertEqual(geo["sparkles"], 9)
        self.assertEqual(len(geo["rings"]), 4)
        plan = splash.build_plan("dark")
        self.assertEqual(sum(1 for op in plan if op["t"] == "polygon"), 12 + 2,
                         "twelve iris blades and the two play-mark facets")
        self.assertEqual(sum(1 for op in plan if op["t"] == "bloom"), 2)
        self.assertEqual(sum(1 for op in plan if op["t"] == "polyline"), 3,
                         "two orbits and the play mark's outline")
        self.assertEqual(sum(1 for op in plan if op["t"] == "text"), len(splash.TEXT))

    def test_the_svg_names_every_text_run_with_its_tracking(self):
        svg = SVG["dark"].read_text(encoding="utf-8")
        for spec in splash.TEXT:
            self.assertIn(f'>{spec["string"]}</text>', svg)
            self.assertIn(f'font-size="{spec["size"]}"', svg)
            self.assertIn(f'letter-spacing="{spec["tracking"]}"', svg)
        self.assertIn("MONARCH", svg)
        self.assertIn("FILM · SERIES · LIVE TELEVISION", svg)
        self.assertIn("EVERY FRAME, IN ITS PLACE", svg)

    def test_the_svg_carries_both_variants_palettes(self):
        for variant in ("dark", "light"):
            design = splash.DESIGN[variant]
            svg = SVG[variant].read_text(encoding="utf-8").lower()
            for _, colour in design["bg"]:
                self.assertIn(colour, svg)
            for _, colour in design["gold"]:
                self.assertIn(colour, svg)
            for key in ("line", "ink", "mid", "muted"):
                self.assertIn(design[key], svg)

    def test_the_two_variants_are_the_same_drawing(self):
        # Same geometry, different paint: the light theme is the engraver's-ink
        # rendering, not a different composition.
        shapes = ("dot", "line", "circle", "polygon", "polyline", "round_rect", "diamond")
        for kind in shapes:
            dark = sum(1 for op in splash.build_plan("dark") if op["t"] == kind)
            light = sum(1 for op in splash.build_plan("light") if op["t"] == kind)
            self.assertEqual(dark, light, f"{kind} count differs between the variants")

    def test_the_plan_is_deterministic(self):
        # The star field is seeded rather than drawn from `random`, so a re-render
        # is byte-identical and a committed PNG can be compared at all.
        self.assertEqual(splash.build_plan("dark"), splash.build_plan("dark"))


class CommittedAsset(unittest.TestCase):
    """The asset is what gets installed, so its shape is asserted, not assumed."""

    def test_each_variant_is_a_4k_rgb_png(self):
        for variant, asset in PNG.items():
            self.assertTrue(asset.is_file(), f"{asset.relative_to(REPO_ROOT)} is not committed")
            self.assertEqual(splash.png_dimensions(asset), splash.SPLASH_SIZE)
            colour_type = asset.read_bytes()[25]
            self.assertIn(colour_type, (2, 6), "expected a truecolour PNG")
        self.assertEqual(splash.SPLASH_SIZE, (3840, 2160))
        self.assertEqual(splash.INSTALLED_NAME, "monarch-splash.png")

    def test_asset_is_a_flat_design_not_a_photo_collage(self):
        # The splash this replaces is Jellyfin's generated collage: 1920×1080 and
        # ~4.1 MB, because it is photographs. A flat design with gradients is
        # fractions of that even at 4K, so an accidental re-import is caught.
        for asset in PNG.values():
            self.assertLess(asset.stat().st_size, 1_500_000,
                            f"{asset.name} is as large as a photo collage — is this "
                            "Jellyfin's generated splash rather than the rendered design?")

    def test_committed_png_matches_a_fresh_render(self):
        if not HAVE_PILLOW:
            self.skipTest("Pillow is not installed; only the committed asset is judged here")
        for variant, asset in PNG.items():
            with TemporaryDirectory() as tmp:
                fresh = Path(tmp) / "fresh.png"
                splash.render(variant, fresh)
                self.assertEqual(splash.png_dimensions(fresh), splash.SPLASH_SIZE)
                self.assertTrue(splash.same_bytes(fresh, asset),
                                f"assets/{asset.name} is not what this design renders — "
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
            asset = splash.SPLASH_ASSET
            self.assertTrue(splash.same_bytes(data / splash.INSTALLED_NAME, asset))
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
            (root / "jellyfin" / "data" / "data" / splash.INSTALLED_NAME).write_bytes(b"not the asset")
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

    def test_render_produces_the_design_at_4k(self):
        if not HAVE_PILLOW:
            self.skipTest("Pillow is not installed")
        from PIL import Image, ImageStat
        with TemporaryDirectory() as tmp:
            dark = Path(tmp) / "dark.png"
            splash.render("dark", dark)
            self.assertEqual(splash.png_dimensions(dark), (3840, 2160))
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
            self.assertEqual(struct.unpack(">II", header[16:24]), (3840, 2160))
            self.assertEqual(header[24:26], b"\x08\x02", "8-bit truecolour RGB, no alpha surprises")


if __name__ == "__main__":
    unittest.main()
