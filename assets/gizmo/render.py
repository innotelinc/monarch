"""Generate the Gizmo splash screen (dark + light, HD + 4K) as SVG → PNG."""

from pathlib import Path

OUT = Path("/tmp/gizmo-splash")
OUT.mkdir(exist_ok=True)

W, H = 1920, 1080

MARK = """
  <g transform="translate(960 400)">
    <rect x="-118" y="-118" width="236" height="236" rx="58"
          fill="url(#badge)" />
    <rect x="-118" y="-118" width="236" height="236" rx="58"
          fill="none" stroke="{badge_stroke}" stroke-width="2" />
    <g transform="translate(0 4)">
      <path d="M 44 -52 A 66 66 0 1 0 66 4"
            fill="none" stroke="{mark_fg}" stroke-width="26"
            stroke-linecap="round" />
      <rect x="10" y="-13" width="58" height="26" rx="13" fill="{mark_fg}" />
    </g>
  </g>
"""

TEXT = """
  <g text-anchor="middle" font-family="Lato, DejaVu Sans, sans-serif">
    <rect x="912" y="646" width="96" height="4" rx="2" fill="{rule}" />
    <text x="{word_x}" y="806" font-size="138" font-weight="700"
          letter-spacing="22" fill="{word_fg}">GIZMO</text>
    <text x="{tag_x}" y="878" font-size="33" font-weight="500"
          letter-spacing="11" fill="{tag_fg}">MOVIES · SERIES · LIVE TV</text>
  </g>
"""

DARK = dict(
    bg="#0B0D10",
    bloom="#6366F1",
    bloom_opacity="0.22",
    badge_stroke="#ffffff",
    badge_stroke_opacity="0.10",
    mark_fg="#FFFFFF",
    rule="#6366F1",
    word_fg="#F6F7FF",
    tag_fg="#98A2B3",
)
LIGHT = dict(
    bg="#F5F6FA",
    bloom="#6366F1",
    bloom_opacity="0.16",
    badge_stroke="#0B0D10",
    badge_stroke_opacity="0.08",
    mark_fg="#FFFFFF",
    rule="#6366F1",
    word_fg="#101319",
    tag_fg="#5A6473",
)


def svg(palette: dict) -> str:
    # text-anchor="middle" ignores trailing letter-spacing, so nudge by half of
    # it to keep the wordmark optically centred.
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}"
     viewBox="0 0 {W} {H}">
  <defs>
    <radialGradient id="bloom" cx="50%" cy="34%" r="62%">
      <stop offset="0%" stop-color="{palette['bloom']}" stop-opacity="{palette['bloom_opacity']}"/>
      <stop offset="60%" stop-color="{palette['bloom']}" stop-opacity="0.04"/>
      <stop offset="100%" stop-color="{palette['bg']}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="badge" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#818CF8"/>
      <stop offset="55%" stop-color="#6366F1"/>
      <stop offset="100%" stop-color="#7C3AED"/>
    </linearGradient>
    <linearGradient id="sheen" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#FFFFFF" stop-opacity="0.16"/>
      <stop offset="100%" stop-color="#FFFFFF" stop-opacity="0"/>
    </linearGradient>
  </defs>

  <rect width="{W}" height="{H}" fill="{palette['bg']}"/>
  <rect width="{W}" height="{H}" fill="url(#bloom)"/>

  <g opacity="0.55">
    <rect x="-118" y="282" width="236" height="236" rx="58" fill="url(#sheen)"
          transform="translate(960 0)"/>
  </g>

  {MARK.format(badge_stroke=palette['badge_stroke'], mark_fg=palette['mark_fg'])}
  {TEXT.format(rule=palette['rule'], word_fg=palette['word_fg'], tag_fg=palette['tag_fg'],
               word_x=971, tag_x=966)}
</svg>
"""


def main() -> None:
    for name, palette in (("dark", DARK), ("light", LIGHT)):
        path = OUT / f"gizmo-splash-{name}.svg"
        path.write_text(svg(palette))
        print(path)


if __name__ == "__main__":
    main()
