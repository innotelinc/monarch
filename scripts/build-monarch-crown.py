#!/usr/bin/env python3
"""build_monarch_crown.py — the Monarch crown, and every file that wears it.

ONE mark, authored once. Everything is drawn from the single CROWN definition
below so the icon, the favicon, the wordmark and the Stalwart domain logo can
never disagree about what the crown looks like.

The crown is a proper open imperial crown: a red velvet cap under two gold
arches that meet at a finial orb, three gold-tipped points, and an jewelled gold
circlet at the base. It replaces the old flat three-peak polygon.

Outputs
-------
Default (`--out`):

    <out>/monarch-crown.svg                the mark, transparent, 128 grid
    <out>/monarch-crown-<n>.png            512/256/192/180/128/64 on transparent
    <out>/monarch-crown-plate.svg/.png     the mark on the dark app plate
    <out>/monarch-crown-white.svg/.png     the flat white mark

With `--bulwark-dir DIR` it also writes the filenames Bulwark 1.14 serves out of
its admin branding directory (`<admin>/branding/`):

    Bulwark_Logo_Color.svg/.png, Bulwark_Logo_White.svg, Bulwark_Favicon.svg,
    Bulwark_Logo_with_Lettering_{White_and_Color,Dark_Color}.svg/.png,
    icon-<n>x<n>.png, icon-maskable-{light,dark}-<n>x<n>.png, apple-touch-icon.png

Rasterising needs rsvg-convert. Everything is written from one definition, so a
re-run against a changed crown updates every file.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HAVE_RSVG = shutil.which("rsvg-convert") is not None

# ── palette ────────────────────────────────────────────────────────────────
GOLD_HI, GOLD, GOLD_MID, GOLD_DEEP, GOLD_EDGE = "#FFF6CF", "#F5CC57", "#DCA22C", "#8A5511", "#9A6512"
RED_HI, RED, RED_DEEP = "#E4404F", "#B5101F", "#5E0A13"
DARK = "#0B0D10"
FONT = "DejaVu Serif"

# ── the crown, on a 128x128 grid ────────────────────────────────────────────
# A cap under two arches meeting at a finial orb, three points, a jewelled
# circlet. `body` carries the gradients; `flat` is the one-colour reading.
CROWN_BODY = """
  <defs>
    <linearGradient id="gold" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{gold_hi}"/><stop offset=".30" stop-color="{gold}"/>
      <stop offset=".62" stop-color="{gold_mid}"/><stop offset=".86" stop-color="#B5791A"/>
      <stop offset="1" stop-color="{gold_deep}"/>
    </linearGradient>
    <linearGradient id="goldRim" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{gold_hi}"/><stop offset=".5" stop-color="#E7B63F"/>
      <stop offset="1" stop-color="{gold_edge}"/>
    </linearGradient>
    <linearGradient id="velvet" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{red_hi}"/><stop offset=".5" stop-color="{red}"/>
      <stop offset="1" stop-color="{red_deep}"/>
    </linearGradient>
    <linearGradient id="ruby" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#FF6A78"/><stop offset=".5" stop-color="#C11427"/>
      <stop offset="1" stop-color="#6E0711"/>
    </linearGradient>
    <linearGradient id="sheen" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity=".5"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <!-- velvet cap -->
  <path d="M23 99 C23 64 41 44 64 44 C87 44 105 64 105 99 Z" fill="url(#velvet)"/>
  <path d="M31 96 C33 69 47 54 61 49 L61 99 Z" fill="url(#sheen)"/>
  <!-- arches over the cap -->
  <path d="M28 99 C32 63 48 48 63 38" fill="none" stroke="url(#goldRim)"
        stroke-width="6.5" stroke-linecap="round"/>
  <path d="M100 99 C96 63 80 48 65 38" fill="none" stroke="url(#goldRim)"
        stroke-width="6.5" stroke-linecap="round"/>
  <!-- finial: orb with a small cross -->
  <rect x="62.4" y="12.6" width="3.2" height="13" rx="1.6" fill="url(#gold)"/>
  <rect x="57.6" y="16.8" width="12.8" height="3.2" rx="1.6" fill="url(#gold)"/>
  <circle cx="64" cy="30" r="9" fill="url(#gold)"/>
  <circle cx="61" cy="27" r="3" fill="{gold_hi}" opacity=".65"/>
  <!-- the three points that show above the circlet -->
  <circle cx="43" cy="62" r="4.2" fill="url(#gold)"/>
  <circle cx="64" cy="50" r="4.6" fill="url(#gold)"/>
  <circle cx="85" cy="62" r="4.2" fill="url(#gold)"/>
  <!-- circlet -->
  <rect x="14" y="98" width="100" height="18" rx="9" fill="url(#gold)"/>
  <rect x="16" y="99.6" width="96" height="4.4" rx="2.2" fill="{gold_hi}" opacity=".7"/>
  <rect x="14" y="110" width="100" height="6" rx="3" fill="{gold_deep}" opacity=".45"/>
  <circle cx="21" cy="107" r="4.6" fill="url(#gold)"/>
  <circle cx="107" cy="107" r="4.6" fill="url(#gold)"/>
  <!-- jewels -->
  <circle cx="35" cy="107" r="4.0" fill="url(#ruby)"/>
  <circle cx="50" cy="107" r="5.0" fill="url(#ruby)"/>
  <circle cx="64" cy="107" r="6.0" fill="url(#ruby)"/>
  <circle cx="78" cy="107" r="5.0" fill="url(#ruby)"/>
  <circle cx="93" cy="107" r="4.0" fill="url(#ruby)"/>
  <circle cx="35" cy="105.6" r="1.3" fill="#FFB3BA" opacity=".85"/>
  <circle cx="50" cy="105.4" r="1.6" fill="#FFB3BA" opacity=".85"/>
  <circle cx="64" cy="105.2" r="1.9" fill="#FFB3BA" opacity=".9"/>
  <circle cx="78" cy="105.4" r="1.6" fill="#FFB3BA" opacity=".85"/>
  <circle cx="93" cy="105.6" r="1.3" fill="#FFB3BA" opacity=".85"/>
""".format(gold_hi=GOLD_HI, gold=GOLD, gold_mid=GOLD_MID, gold_deep=GOLD_DEEP,
           gold_edge=GOLD_EDGE, red_hi=RED_HI, red=RED, red_deep=RED_DEEP)

# Flat white mark, for a coloured surface: the same silhouette, one colour, and
# the jewels as soft holes rather than a second colour.
CROWN_FLAT = """
  <g fill="{white}">
    <path d="M23 99 C23 64 41 44 64 44 C87 44 105 64 105 99 Z"/>
    <path d="M28 99 C32 63 48 48 63 38" fill="none" stroke="{white}" stroke-width="6.5" stroke-linecap="round"/>
    <path d="M100 99 C96 63 80 48 65 38" fill="none" stroke="{white}" stroke-width="6.5" stroke-linecap="round"/>
    <rect x="62.4" y="12.6" width="3.2" height="13" rx="1.6"/>
    <rect x="57.6" y="16.8" width="12.8" height="3.2" rx="1.6"/>
    <circle cx="64" cy="30" r="9"/>
    <circle cx="43" cy="62" r="4.2"/><circle cx="64" cy="50" r="4.6"/><circle cx="85" cy="62" r="4.2"/>
    <rect x="14" y="98" width="100" height="18" rx="9"/>
    <circle cx="21" cy="107" r="4.6"/><circle cx="107" cy="107" r="4.6"/>
  </g>
  <g fill="{hole}" opacity=".55">
    <circle cx="35" cy="107" r="2.4"/><circle cx="50" cy="107" r="3.0"/>
    <circle cx="64" cy="107" r="3.6"/><circle cx="78" cy="107" r="3.0"/><circle cx="93" cy="107" r="2.4"/>
  </g>
"""


def mark(body: str, size: int = 128, scale: float = 1.0, bg: str = "") -> str:
    """The crown on a square canvas, optionally on a rounded plate."""
    plate = (f'<rect width="{size}" height="{size}" rx="{int(size * 0.22)}" fill="{bg}"/>'
             if bg else "")
    w = 128 * scale
    inset = (size - w) / 2
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'width="{size}" height="{size}" role="img" aria-label="Monarch">\n'
            f'  <title>Monarch</title>\n  {plate}\n'
            f'  <g transform="translate({inset:g} {inset:g}) scale({scale:g})">{body}</g>\n</svg>\n')


def lettering(body: str, text_fill: str) -> str:
    """550x100 lock-up: the crown then INNOTEL MAIL with a gold rule under it."""
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 560 100" '
            'width="560" height="100" role="img" aria-label="Innotel Mail">\n'
            '  <title>Innotel Mail</title>\n'
            f'  <g transform="translate(4 8) scale(0.66)">{body}</g>\n'
            f'  <text x="102" y="46" font-family="{FONT}" font-weight="bold" '
            f'font-size="37" letter-spacing="2.2" fill="{text_fill}" '
            'dominant-baseline="middle">INNOTEL MAIL</text>\n'
            '  <text x="104" y="70" font-family="DejaVu Sans" font-size="11.5" '
            f'letter-spacing="6.4" fill="{GOLD}" opacity=".95">SELF-HOSTED MAIL</text>\n'
            f'  <rect x="104" y="80" width="360" height="2.6" rx="1.3" fill="{GOLD}" opacity=".8"/>\n'
            '</svg>\n')


def raster(svg: str, out: "Path", w: int, h: int) -> None:
    """Rasterise, when rsvg-convert is present. The SVG is the source of truth,
    so a host without it still gets a complete SVG set rather than a crash."""
    if not HAVE_RSVG:
        return
    tmp = out.with_suffix(".src.svg")
    tmp.write_text(svg)
    try:
        proc = subprocess.run(["rsvg-convert", "-w", str(w), "-h", str(h), "-o", str(out), str(tmp)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(f"rsvg-convert failed for {out.name}: {proc.stderr.strip()}")
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent.parent / "assets")
    ap.add_argument("--bulwark-dir", type=Path,
                    help="also write the Bulwark branding filenames here")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if not HAVE_RSVG:
        print("no rsvg-convert on this host: writing SVGs only "
              "(install librsvg2-bin to regenerate the PNGs)", file=sys.stderr)

    # The mark first — the canonical asset.
    (args.out / "monarch-crown.svg").write_text(mark(CROWN_BODY))
    (args.out / "monarch-crown-white.svg").write_text(mark(CROWN_FLAT.format(white="#FFFFFF", hole="#0B0D10")))
    (args.out / "monarch-crown-plate.svg").write_text(mark(CROWN_BODY, scale=0.84, bg=DARK))
    for n in (512, 256, 192, 180, 128, 64):
        raster(mark(CROWN_BODY), args.out / f"monarch-crown-{n}.png", n, n)
    raster(mark(CROWN_BODY, scale=0.84, bg=DARK), args.out / "monarch-crown-plate-512.png", 512, 512)
    raster(mark(CROWN_FLAT.format(white="#FFFFFF", hole="#0B0D10")), args.out / "monarch-crown-white-512.png", 512, 512)
    print(f"crown            {args.out}/monarch-crown.svg"
          + (" + 6 png" if HAVE_RSVG else " (no rsvg-convert: SVG only)"))
    for name in ("monarch-crown.svg", "monarch-crown-512.png", "monarch-crown-plate.svg",
                 "monarch-crown-plate-512.png", "monarch-crown-white.svg"):
        path = args.out / name
        if path.exists():
            print(f"  {name:32s} {path.stat().st_size:>7d} bytes")

    if args.bulwark_dir and not HAVE_RSVG:
        print("skipping the Bulwark PNG set (no rsvg-convert)", file=sys.stderr)
    if args.bulwark_dir:
        d = args.bulwark_dir
        d.mkdir(parents=True, exist_ok=True)
        svgs = {
            "Bulwark_Logo_Color.svg": mark(CROWN_BODY),
            "Bulwark_Favicon.svg": mark(CROWN_BODY),
            "Bulwark_Logo_White.svg": mark(CROWN_FLAT.format(white="#FFFFFF", hole="#0B0D10")),
            "Bulwark_Logo_with_Lettering_White_and_Color.svg": lettering(CROWN_BODY, "#FFFFFF"),
            "Bulwark_Logo_with_Lettering_Dark_Color.svg": lettering(CROWN_BODY, DARK),
            "icon-512x512.svg": mark(CROWN_BODY, scale=0.84, bg=DARK),
            "icon-maskable-light-512x512.svg": mark(CROWN_BODY, scale=0.66, bg="#FFFFFF"),
            "icon-maskable-dark-512x512.svg": mark(CROWN_BODY, scale=0.66, bg=DARK),
        }
        for name, svg in svgs.items():
            (d / name).write_text(svg)
        raster(mark(CROWN_BODY), d / "Bulwark_Logo_Color.png", 1000, 1000)
        raster(lettering(CROWN_BODY, "#FFFFFF"), d / "Bulwark_Logo_with_Lettering_White_and_Color.png", 1650, 300)
        raster(lettering(CROWN_BODY, DARK), d / "Bulwark_Logo_with_Lettering_Dark_Color.png", 3300, 600)
        raster(mark(CROWN_BODY, scale=0.84, bg=DARK), d / "icon-512x512.png", 512, 512)
        raster(mark(CROWN_BODY, scale=0.84, bg=DARK), d / "icon-192x192.png", 192, 192)
        raster(mark(CROWN_BODY, scale=0.84, bg=DARK), d / "apple-touch-icon.png", 180, 180)
        raster(mark(CROWN_BODY, scale=0.66, bg="#FFFFFF"), d / "icon-maskable-light-512x512.png", 512, 512)
        raster(mark(CROWN_BODY, scale=0.66, bg="#FFFFFF"), d / "icon-maskable-light-192x192.png", 192, 192)
        raster(mark(CROWN_BODY, scale=0.66, bg=DARK), d / "icon-maskable-dark-512x512.png", 512, 512)
        raster(mark(CROWN_BODY, scale=0.66, bg=DARK), d / "icon-maskable-dark-192x192.png", 192, 192)
        print(f"\nbulwark set      {len(svgs)} svg"
              + (" + 10 png" if HAVE_RSVG else " (no rsvg-convert: SVG only)")
              + f" -> {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
