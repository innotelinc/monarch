#!/usr/bin/env python3
"""jellyfin-background.py — Monarch's backdrop, reproduced from the repo.

WHY THIS EXISTS
---------------
Jellyfin has exactly one background surface this repo owns: the **branding**
configuration. `BrandingOptions.CustomCss` is served verbatim at
`/Branding/Css` (measured on the media host: `200 text/css`, the same bytes the
web client injects), and it is the only place the server will carry custom CSS.
The login page shipped a flat `#08090b` field, and the splash
(`scripts/jellyfin-splash.py`) handed the monogram over to a screen that dropped
it — the obsidian/champagne system stopped at the door.

HOW THE IMAGE REACHES THE CLIENT
--------------------------------
No route serves a file out of Jellyfin's data directory: `/Branding/Splashscreen`
is a special case for the splash image, not a pattern. So the backdrop travels
as an SVG **data URI inside the CSS**, base64'd by this script from the committed
asset (`assets/monarch-background.svg`, or the `-light` variant), which keeps the
served CSS reproducible from the repo instead of hand-pasted, and keeps the
stylesheet self-contained — one request, no second origin, nothing to 404.

Two rules carry it, and the selectors are deliberate:

* `html body` — the backdrop for the app.
* `html body #loginPage` — the login screen. The SSO block in `LoginDisclaimer`
  sets `#loginPage{background:#08090b!important}`, and a bare `#loginPage` rule
  would be a coin toss on document order; this one carries two type selectors
  more, so it wins wherever Jellyfin injects the two stylesheets.

Everything else in `CustomCss` is the palette the app already wore (chrome panel,
champagne links, button radius). `LoginDisclaimer`, `SplashscreenEnabled` and
`SplashscreenLocation` are read and written back untouched — this script is not
allowed to disturb the sign-in block or the splash.

USAGE
-----
    python3 scripts/jellyfin-background.py --check        # report only (default)
    python3 scripts/jellyfin-background.py --apply        # write the CSS into branding
    python3 scripts/jellyfin-background.py --apply --variant light
    python3 scripts/jellyfin-background.py --check --url http://127.0.0.1:8096

Runs on a host that mounts the media host's appdata (`APPDATA`, default
`/docker/appdata`): the config it edits is `$APPDATA/jellyfin/branding.xml` —
the file the server reads as `/config/branding.xml`. Paths:

    asset           assets/monarch-background[-light].svg   (this repo)
    config          $APPDATA/jellyfin/branding.xml          (Jellyfin's /config)
    served          GET /Branding/Css                       (--url, the client's copy)

A written file is what the server reads at boot, but a running server does **not**
reload it: measured on the media host, `/Branding/Css` kept serving the old
stylesheet after `branding.xml` changed, and switched only when the same config
was posted back through `POST /System/Configuration/branding` (that DTO carries
`CustomCss`/`LoginDisclaimer`/`SplashscreenEnabled` and left the recorded
`SplashscreenLocation` alone). So `--url` reports what a client is handed rather
than assuming a reload, and a restart is the fallback.

Exit codes: 0 = the branded backdrop is applied and matches the repo asset;
1 = it is not (a finding is printed); 2 = cannot run (no appdata, no asset).
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = REPO_ROOT / "assets"

# Palette, per variant. The dark numbers are the app's existing CustomCss; the
# light ones are `assets/monarch-splash-light.svg`'s ivory/bronze pair, so the
# two variants of the brand stay in step.
PALETTE = {
    "dark": {
        "asset": "monarch-background.svg",
        "base": "#08090b",
        "ink": "#f5f0e4",
        "accent": "#d8b56d",
        "hover": "#caa35d",
        "panel": "#101214",
        "line": "#2b2d30",
    },
    "light": {
        "asset": "monarch-background-light.svg",
        "base": "#faf7ef",
        "ink": "#25231f",
        "accent": "#8e6b36",
        "hover": "#caa35d",
        "panel": "#f2ece0",
        "line": "#ded4c2",
    },
}

BRANDING_FILE = "branding.xml"
DEFAULT_URL = "http://127.0.0.1:8096"

class CannotRun(RuntimeError):
    """Not enough on disk to do the job (no appdata, no asset)."""


def build_css(uri: str, variant: str) -> str:
    """The whole stylesheet the server should serve, from the asset's data URI."""
    palette = PALETTE[variant]
    base, ink = palette["base"], palette["ink"]
    accent, hover = palette["accent"], palette["hover"]
    panel, line = palette["panel"], palette["line"]
    backdrop = f'url("{uri}") center/cover no-repeat fixed'
    rules = [
        # `html body` outranks Jellyfin's own `body` rules and nothing else.
        f"html body{{background:{base} {backdrop}!important;color:{ink}!important}}",
        # Two type selectors more than the SSO block's `#loginPage` rule, so the
        # login screen keeps the backdrop whichever stylesheet lands last.
        f"html body #loginPage{{background:{base} {backdrop}!important}}",
        f"a{{color:{accent}!important}}",
        f".headerTop{{background:{panel}!important;border-bottom:1px solid {line}!important}}",
        f".mainDrawer,.nowPlayingBar{{background:{panel}!important}}",
        ".emby-button,.button-submit{border-radius:4px!important}",
        f".raised:hover{{background:{hover}!important;color:{base}!important}}",
    ]
    return "".join(rules)


def asset_uri(asset: Path) -> str:
    """The committed SVG as a CSS data URI. Reads the file, never a copy."""
    payload = asset.read_bytes()
    if b"<svg" not in payload:
        raise CannotRun(f"{asset} does not look like an SVG")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def branding_path(config_dir: Path) -> Path:
    return config_dir / BRANDING_FILE


def read_branding(path: Path) -> dict[str, str]:
    """(custom_css, login_disclaimer) as the file holds them, or empty strings."""
    if not path.is_file():
        return {"css": "", "disclaimer": "", "splash": ""}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise CannotRun(f"{path} is not valid XML ({error})") from None
    return {
        "css": (root.findtext("CustomCss") or "").strip(),
        "disclaimer": (root.findtext("LoginDisclaimer") or "").strip(),
        "splash": (root.findtext("SplashscreenLocation") or "").strip(),
    }


def write_css(path: Path, css: str) -> None:
    """Replace only `<CustomCss>`; every other element is written back as it was."""
    if not path.is_file():
        raise CannotRun(f"{path} does not exist — is Jellyfin initialised?")
    try:
        tree = ET.parse(path)
    except ET.ParseError as error:
        raise CannotRun(f"{path} is not valid XML ({error})") from None
    root = tree.getroot()
    element = root.find("CustomCss")
    if element is None:
        element = ET.SubElement(root, "CustomCss")
    element.text = css
    body = ET.tostring(root, encoding="unicode")
    path.write_text("<?xml version='1.0' encoding='utf-8'?>\n" + body + "\n",
                    encoding="utf-8")


def served_css(url: str) -> str:
    """What a client is actually handed, so the check is not just the file."""
    request = urllib.request.Request(f"{url.rstrip('/')}/Branding/Css")
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", "replace").strip()


def check(args) -> int:
    config_dir = Path(args.config_dir)
    if not config_dir.is_dir():
        raise CannotRun(f"{config_dir} does not exist — is this the media host's appdata? "
                        "(set APPDATA, or pass --config-dir)")

    variant = args.variant
    palette = PALETTE[variant]
    asset = Path(args.asset) if args.asset else ASSET_DIR / palette["asset"]
    path = branding_path(config_dir)
    findings = []

    if not asset.is_file():
        findings.append(f"{asset} is missing — restore the committed asset")
        expected, uri = "", ""
    else:
        try:
            uri = asset_uri(asset)
        except CannotRun as error:
            findings.append(str(error))
            expected, uri = "", ""
        else:
            expected = build_css(uri, variant)

    live = read_branding(path)
    if not path.is_file():
        findings.append(f"{path} does not exist — Jellyfin has never been initialised")
    elif not live["css"]:
        findings.append(f"{path} has no CustomCss — the app is wearing Jellyfin's theme, "
                        "not Monarch's")
    elif expected and live["css"] != expected:
        detail = "the backdrop image is missing from the served CSS" \
            if uri and uri not in live["css"] else "the CSS differs from this repo's asset"
        findings.append(f"{path} CustomCss does not match {asset.name} — {detail}")

    # Only claimed when the fetch actually happened: an unreachable URL is a note,
    # not evidence, and the success line must not borrow it (the server's port is
    # not published on every host, which is exactly when this path is taken).
    served_note = ""
    if args.url and not findings:
        try:
            served = served_css(args.url)
        except (urllib.error.URLError, OSError) as error:
            print(f"  note  {args.url} is not reachable from here ({error}); "
                  "the file above is what the server reads at /config/branding.xml")
        else:
            if served != live["css"]:
                findings.append(f"{args.url}/Branding/Css does not match {path} — the server "
                                "is serving a different stylesheet than the file says")
            elif uri not in served:
                findings.append(f"{args.url}/Branding/Css carries no backdrop data URI")
            else:
                served_note = f", served at {args.url}/Branding/Css"

    for finding in findings:
        print(f"  FAIL  {finding}")
    if findings:
        print(f"\nFAIL — {len(findings)} problem(s): Jellyfin is not showing Monarch's "
              "backdrop. Apply it with: python3 scripts/jellyfin-background.py --apply")
        return 1
    print(f"  ok    {path} carries {asset.name} ({variant}) as the backdrop and nothing "
          f"else differs{served_note}")
    return 0


def apply(args) -> int:
    variant = args.variant
    palette = PALETTE[variant]
    asset = Path(args.asset) if args.asset else ASSET_DIR / palette["asset"]
    if not asset.is_file():
        raise CannotRun(f"{asset} is missing — restore the committed asset")
    uri = asset_uri(asset)
    css = build_css(uri, variant)

    path = branding_path(Path(args.config_dir))
    before = read_branding(path)
    write_css(path, css)
    print(f"wrote        {path} CustomCss ({len(css)} bytes, {asset.name})")
    untouched = [name for name, value in (("LoginDisclaimer", before["disclaimer"]),
                                          ("SplashscreenLocation", before["splash"]))
                 if value]
    if untouched:
        print(f"preserved    {', '.join(untouched)}")
    if args.url:
        try:
            served = served_css(args.url)
        except (urllib.error.URLError, OSError) as error:
            print(f"  note  {args.url} is not reachable from here ({error}); the server "
                  "reloads branding from this file, verify with --check --url later")
        else:
            state = "carries the new backdrop" if uri in served else "is still the old CSS"
            print(f"checked      {args.url}/Branding/Css {state}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report only (the default)")
    parser.add_argument("--apply", action="store_true", help="write the backdrop CSS into branding")
    parser.add_argument("--variant", choices=sorted(PALETTE), default="dark")
    parser.add_argument("--asset", default="", help="the SVG to embed (default assets/<variant>.svg)")
    parser.add_argument("--appdata", default=os.environ.get("APPDATA", "/docker/appdata"))
    parser.add_argument("--config-dir", default="",
                        help="Jellyfin's config dir (default <appdata>/jellyfin)")
    parser.add_argument("--url", default="",
                        help=f"also confirm the served CSS (e.g. {DEFAULT_URL})")
    args = parser.parse_args(argv)
    if not args.config_dir:
        args.config_dir = str(Path(args.appdata) / "jellyfin")

    try:
        return apply(args) if args.apply else check(args)
    except CannotRun as error:
        print(f"jellyfin-background: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
