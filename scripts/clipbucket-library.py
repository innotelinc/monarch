#!/usr/bin/env python3
"""clipbucket-library.py — the media library, as ClipBucket's catalogue.

WHY THIS EXISTS
---------------
`scripts/clipbucket-install.py` finishes the install, and a finished install is
an *empty* site: measured 2026-09-22, `cb_video` held 0 rows against a
`files/videos/` holding only the app's own `example.mp4`. The estate's video
content lives in the Jellyfin library (`/data/media/movies`, `/data/media/tv`
on the media host), and nothing carried it across — so `tube.innotel.us`
answered a working site with nothing in it, which reads exactly like a broken
import.

Two things make this less obvious than "copy the files in":

1. **ClipBucket will not list a file.** A video is a `cb_video` row *and* a file
   at a path the app derives from that row, *and* a default-thumbnail row that
   has to exist before the browse page renders a card. An mp4 dropped into
   `files/videos/` is invisible; a row without its file is a dead link; a row
   without its thumbnail is a card with a broken image. So this tool writes all
   three, or none.
2. **The library is not web-playable as it stands.** Measured across the 15
   files: 2 are H.264/AAC MP4 (playable as they are), 8 are H.264 in an MKV
   (browsers will not demux Matroska), and 5 are **HEVC**, which plays only
   where the client has an HEVC decoder. The container and audio half of that is
   cheap to fix and is fixed here; the HEVC half of it is a codec the client may
   not decode, and this tool's answer is to **keep the codec** rather than
   spend hours replacing it: a stream copy costs seconds and no quality, and
   **the count is reported** (by `--check` and by `--apply`) so the limitation is
   a recorded number rather than a surprise. That number is on `.46` and on
   `.56`; an earlier version of this tool re-encoded those seven items on `.56`
   and five on `.46`, and this one treats a copy that is not a stream copy of
   its source as drift and repairs it.

WHAT IT DOES
------------
For every movie and TV episode it can recognise under the media root:

* **Media** — a web-playable MP4 at
  `files/videos/<MEDIA_DIR>/<file_name>-<quality>.mp4` with
  `cb_video.video_files` set to that quality, which is the name the app's own
  `get_video_files()` builds and `update_video_files()` parses back — by
  hardlink when the source is already H.264/AAC MP4 (no copy at all — the media
  root and the docker volume share a filesystem here), otherwise by a stream
  copy (`-c:v copy`) with the audio converted to stereo AAC. **Video is never
  re-encoded** — the copy keeps the source's video codec, and a copy whose
  codec does not match its source's (one an earlier version transcoded, or one
  written before the source was replaced) is rebuilt as a stream copy, which is
  the repair for exactly that. The only exception is a source codec no browser
  decodes at all, where there is no cheaper way to make it playable. The source
  library is read-only
  and is never modified; a file this tool wrote under a name it no longer uses
  is *renamed*, never deleted.
* **Thumbnails** — the five the app itself would generate (`num_thumbs`), each
  at the five resolutions `VideoThumbs::$resolution_setting` declares, named by
  `VideoThumbs::getThumbName()` and inserted into `cb_video_image` /
  `cb_video_thumb`, using the app's own ffmpeg command
  (`FFMpeg::extractVideoThumbnail`: `-pix_fmt yuvj422p`, webp, the same
  scale/pad filter). `cb_video.default_thumbnail` points at the first one.
* **Catalogue rows** — one `cb_video` row per item, in the state the browse
  query requires (`status='Successful'`, `active='yes'`,
  `broadcast='public'`, `subscription_email='pending'`), filed under a
  **Movies** or **TV Shows** category, with the show name as a tag so a series
  is reachable as a set.

**The identity of an item is its path.** `file_name` is derived from the source
path (a readable slug plus a short digest), so re-running converges instead of
duplicating, a metadata edit in the database is not undone, and renaming a
display title does not orphan the file.

USAGE
-----
    python3 scripts/clipbucket-library.py --check          # what is missing
    python3 scripts/clipbucket-library.py --apply          # import everything
    python3 scripts/clipbucket-library.py --apply --only tv
    python3 scripts/clipbucket-library.py --apply --limit 2 # a first look
    python3 scripts/clipbucket-library.py --serve-check    # does the site serve it?

`--check` is read-only and exits 1 when the catalogue is behind the library,
which is what makes it usable from a drift check. `--apply` is idempotent: an
item already in the catalogue keeps its rows and only has missing files,
thumbnails or metadata restored.

`--serve-check` is the end-to-end half, and the only check that asks the *app*
rather than this tool's model of it: it fetches every item's watch page, takes
the `<source>` URLs the page emits, and requires a file behind each one that the
web server will actually serve bytes from. Everything else here can pass while
the site serves nothing — that is exactly how the first import's
`<name>-<hash>.mp4` rows looked complete and played nowhere.

Exit codes: 0 in sync (or applied, or every item served) — 1 behind/drift, or a
file this tool will not guess about — 2 cannot tell (no container, no docker, no
ffmpeg, or a media root that is not there).

The deployment's own state is not in this repo: the media root is named by
`--media-root` / `CLIPBUCKET_MEDIA_ROOT` and defaults to `/data/media`.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass

CONTAINER_DEFAULT = "clipbucket"
# Where the container's file volume lands, and the web root inside it (see
# clipbucket/nginx-clipbucket.conf).
FILES_VOLUME_DEST = "/srv/http/clipbucket"
MEDIA_ROOT_DEFAULT = "/data/media"
MOVIES_SUBDIR_DEFAULT = "movies"
TV_SUBDIR_DEFAULT = "tv"

DB_NAME = "clipbucket"
DB_USER = "root"

# Every video this tool makes shares one `file_directory`, so the import is one
# name in the database and one directory on disk: "what did the importer
# create" is then a query and an `rm -rf`, not an archaeology exercise. Kept
# short because `cb_video.file_directory` is varchar(25).
MEDIA_DIR = "imported"

# The version the installed release claims. Checked against `cb_version` rather
# than assumed: the app gates columns and queries on it, and `video_version` has
# to match what the app believes it is running.
CORE_VERSION = "5.5.3"
VIDEO_CATEGORY_TYPE = 1  # cb_object_type: 1 = video
VIDEO_TAG_TYPE = 1  # cb_tags_type: 1 = video
UPLOADER_USERID = 1
UPLOADER_NAME = "admin"

# The app's own limits and thumbnail vocabulary, read from the deployment
# (cb_config max_video_title / num_thumbs / video_thumbs_format and
# VideoThumbs::$resolution_setting). Constants so the rendering below is
# testable without a database; `--check` compares the install's version against
# CORE_VERSION and refuses to judge when they disagree.
MAX_FILE_NAME = 32  # cb_video.file_name varchar(32)
BASE_LENGTH = MAX_FILE_NAME - 10  # the readable half, leaving room for "-" + the digest
MAX_TITLE = 80  # cb_config max_video_title
NUM_THUMBS = 5  # cb_config num_thumbs
THUMB_FORMAT = "webp"  # cb_config video_thumbs_format
THUMB_BACKGROUND = "black"  # cb_config thumb_background_color, as ffmpeg spells it
DEFAULT_THUMB_NUM = 1

# VideoThumbs::$resolution_setting['thumbnail'] — (size_tag, width, height).
# The tag is what appears in the file name; 'original' keeps the source frame.
THUMB_RESOLUTIONS = (
    ("original", None, None),
    ("168x105", 168, 105),
    ("416x260", 416, 260),
    ("512x320", 512, 320),
    ("768x480", 768, 480),
)

# The heights ClipBucket converts to, and therefore the labels it gives a
# converted file: a video's playable files are `cb_video.video_files`, a JSON
# array of these, and the file behind `video_files=[1080]` is
# `files/videos/<dir>/<file_name>-1080.mp4` (functions_video.php:
# `get_video_files` builds that name and `update_video_files` parses it back out
# of the directory listing). A file named anything else is either invisible or
# read as a resolution.
QUALITY_LADDER = (1080, 720, 480, 360, 240)

# Codecs a browser will play from an MP4 unaided. An MKV or an odd audio codec
# is remuxed (video copied) or converted (audio); HEVC is kept as HEVC and
# *reported*, because a stream copy of it is playable only where the client can
# decode HEVC. Nothing here re-encodes a video on purpose.
WEB_VIDEO = ("h264",)
WEB_AUDIO = ("aac",)
HEVC = "hevc"

# ffprobe names a container by every format it could be, comma-joined, so an
# .mp4 reports `mov,mp4,m4a,3gp,3g2,mj2` — testing the string for equality with
# "mp4" is false for every MP4 ever written, which is how the hardlink branch
# below came to be dead code on the first pass over this library.
MP4_CONTAINERS = frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"})

VIDEO_SUFFIXES = (
    ".mp4", ".mkv", ".avi", ".m4v", ".mov", ".webm", ".wmv",
    ".flv", ".ts", ".mpg", ".mpeg",
)
QUALITY_RE = re.compile(
    r"\s+(?:HDTV|WEBDL|WEB-?DL|WEBRip|BluRay|BDRip|BRRip|REMUX|DVDRip|HDTVRip|PDTV|AMZN|DSNP|ATVP)\b.*$",
    re.IGNORECASE,
)
RESOLUTION_RE = re.compile(r"\s+\d{3,4}[pi]\b.*$", re.IGNORECASE)
EPISODE_RE = re.compile(r"^[sS](\d{1,2})[eE](\d{1,3})$")


class CantTell(Exception):
    """Nothing could be evaluated — not a pass, and not a finding either."""


class ImportError_(RuntimeError):
    """The apply could not be completed."""


# ── pure rendering: names, titles, and the media decision ───────────────────


def slugify(text: str) -> str:
    """A readable, file-safe slug: lowercase, ASCII, single dashes."""
    text = text.replace("&", " and ")
    text = "".join(ch if ch.isalnum() else "-" for ch in text)
    text = re.sub(r"-{2,}", "-", text).strip("-").lower()
    return text


def trim(text: str, limit: int) -> str:
    """`text` cut to `limit`, preferring not to end mid-word or on a separator."""
    if len(text) <= limit:
        return text
    cut = re.sub(r"[\s\-–—/,:;.]+$", "", text[:limit].rstrip())
    return cut or text[:limit]


def fit_title(text: str, limit: int = MAX_TITLE) -> str:
    """A title the app will accept — it validates against `max_video_title`."""
    return trim(text, limit)


def strip_quality(text: str) -> str:
    """Drop the release tags a file name carries and a title should not."""
    text = QUALITY_RE.sub("", text)
    text = RESOLUTION_RE.sub("", text)
    return text.strip(" -_")


def parse_episode_name(stem: str) -> tuple[str, int, int, str] | None:
    """`Show - S01E02 - Title WEBDL-1080p` → (show, season, episode, title).

    The season/episode token is *searched for* rather than assumed to be the
    second field: a show whose own name contains the separator
    (`Star Trek - Strange New Worlds - S04E02 - …`) puts it third, and reading
    positionally would file that episode under "Star Trek" with a season of
    zero. A name with no token at all returns None — `scan_library` reports
    those instead of guessing, because a guessed title is one nobody chose.
    """
    parts = [p.strip() for p in stem.split(" - ")]
    for index, part in enumerate(parts):
        match = EPISODE_RE.match(part)
        if not match:
            continue
        show = " - ".join(parts[:index]).strip()
        title = strip_quality(" - ".join(parts[index + 1:])) if index + 1 < len(parts) else ""
        return show, int(match.group(1)), int(match.group(2)), title
    return None


def render_title(kind: str, name: str, season: int, episode: int, episode_title: str) -> str:
    """The `cb_video.title` for an item, always inside `max_video_title`.

    A full episode title is used when it fits; otherwise the title falls back to
    the show and the episode number rather than being cut mid-sentence, because
    the fallback is *complete* and a truncated title is not.
    """
    if kind != "episode":
        return fit_title(name)
    head = f"{name} - S{season:02d}E{episode:02d}"
    if episode_title:
        with_title = f"{head} - {episode_title}"
        if len(with_title) <= MAX_TITLE:
            return with_title
    return fit_title(head)


def file_name_for(relpath: str, base: str) -> str:
    """The stable on-disk/database name for a library item. len <= 32.

    Readable first, then a digest of the *path*: the digest is what keeps the
    name stable when a display title changes, and unique when two shows share a
    truncated prefix. Length is `cb_video.file_name`'s varchar(32).
    """
    return f"{trim(base, BASE_LENGTH) or 'item'}-{hashlib.sha1(relpath.encode()).hexdigest()[:8]}"


def media_decision(video_codec: str, audio_codec: str, container: str) -> str:
    """`link` (no work), `remux` (container/audio only), or `transcode`.

    `container` is ffprobe's `format_name` as reported — comma-joined when the
    format is ambiguous (`.mp4` is `mov,mp4,m4a,3gp,3g2,mj2`), so it is read as a
    set. Pure, and it takes no "how hard may I try" argument on purpose: the
    answer is decided by the source alone, so an import is seconds or minutes
    and never hours because of a flag somebody left on. `transcode` survives for
    the one case with no cheaper option — a video codec no browser decodes — and
    is unreachable for HEVC, which is remuxed.
    """
    containers = {part.strip().lower() for part in str(container).split(",")}
    if video_codec in WEB_VIDEO and audio_codec in WEB_AUDIO and containers & MP4_CONTAINERS:
        return "link"
    if video_codec not in WEB_VIDEO and video_codec != HEVC:
        # A stream copy of something no browser decodes is not an import. The
        # re-encode is silent here only because there is no cheaper option.
        return "transcode"
    # HEVC included: a stream copy with the audio fixed is playable wherever the
    # client decodes HEVC, and re-encoding it is the thing this tool no longer
    # does at all.
    return "remux"


def expected_codec(video_codec: str, decision: str) -> str:
    """The video codec a faithful copy of this source carries.

    `link` and `remux` copy the stream, so the codec is the source's; a
    `transcode` produces H.264. This is what a copy already in place is judged
    against, which is what makes "a stream copy of the source" an invariant
    rather than an intention.
    """
    return "h264" if decision == "transcode" else video_codec


def quality_for(width: int, height: int) -> int:
    """The resolution label the app would give this frame.

    ClipBucket labels a converted file with its height, but a letterboxed
    1920x804 film is 1080p class rather than 804p, so the label comes from the
    16:9-equivalent height and is snapped to the ladder the app converts to —
    which keeps `video_files` a value the app itself could have written.
    """
    if width <= 0:
        width = height
    equivalent = max(height, round(width * 9 / 16)) if width else height
    for step in QUALITY_LADDER:
        if equivalent >= step:
            return step
    return QUALITY_LADDER[-1]


def media_name(file_name: str, quality: int) -> str:
    """The on-disk name of a playable file — the name `get_video_files` builds."""
    return f"{file_name}-{quality}.mp4"


def media_files(videos_dir: str, file_name: str) -> list[str]:
    """Every playable file written for one item, under any name."""
    if not os.path.isdir(videos_dir):
        return []
    return sorted(
        n for n in os.listdir(videos_dir) if n.startswith(file_name) and n.endswith(".mp4")
    )


MEDIA_NAME_RE = re.compile(r"^.*-\d+\.mp4$")


def named_for_the_app(found: list[str]) -> bool:
    """Is at least one of these a name `get_video_files()` will build?

    A file without the `-<quality>` tail is not "there" as far as the player is
    concerned: `update_video_files` reads the tail as the resolution, so a bare
    `<file_name>.mp4` becomes a quality of whatever the name ends with and the
    file the player then requests does not exist.
    """
    return any(MEDIA_NAME_RE.match(name) for name in found)


def stale_media(videos_dir: str, file_name: str, keep: str) -> list[str]:
    """Files this tool made for this item under a name it no longer uses.

    There is exactly one reason for these to exist: the naming changed between
    runs (which is how the first version of this script got it wrong — a bare
    `<file_name>.mp4` is read by `update_video_files` as a *resolution*, and the
    player then asks for a second file that was never written). Renaming is the
    repair, because the media itself is fine; nothing here deletes a video.
    """
    if not os.path.isdir(videos_dir):
        return []
    return sorted(
        name
        for name in os.listdir(videos_dir)
        if name.startswith(file_name) and name.endswith(".mp4") and name != keep
    )


def media_argv(source: str, dest: str, decision: str, video_codec: str) -> list[str]:
    """The ffmpeg command for a media decision. Never touches `source`."""
    argv = ["ffmpeg", "-y", "-v", "error", "-i", source, "-map", "0:v:0", "-map", "0:a:0?"]
    if decision == "transcode":
        argv += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    else:
        argv += ["-c:v", "copy"]
        if video_codec == HEVC:
            # The hvc1 tag is what makes an HEVC MP4 playable on Apple clients.
            argv += ["-tag:v", "hvc1"]
    argv += [
        "-c:a", "aac", "-ac", "2", "-b:a", "192k",
        "-sn", "-dn", "-movflags", "+faststart", dest,
    ]
    return argv


def thumb_argv(
    source: str, dest: str, at_seconds: int, size_tag: str,
    width: int | None, height: int | None,
) -> list[str]:
    """One thumbnail, with the app's own filter (`FFMpeg::extractVideoThumbnail`).

    `-ss` before `-i` is what ClipBucket does, and it is what makes this cheap:
    it seeks instead of decoding from the start of a two-hour film.
    """
    argv = [
        "ffmpeg", "-y", "-v", "error", "-ss", str(at_seconds), "-i", source,
        "-an", "-pix_fmt", "yuvj422p",
    ]
    if size_tag != "original":
        ratio = f"{width}/{height}"
        argv += [
            "-vf",
            (
                f"scale='if(gt(a,{ratio}),{width},-1)':'if(gt(a,{ratio}),-1,{height})',"
                f"pad={width}:{height}:({width}-iw)/2:({height}-ih)/2:{THUMB_BACKGROUND}"
            ),
        ]
    argv += ["-c:v", "libwebp", "-quality", "80", "-frames:v", "1", dest]
    return argv


def thumb_num(i: int) -> str:
    """`VideoThumbs::generateThumbNum()` for a 5.5.3 install: zero-padded to 5."""
    return str(i).zfill(5)


def thumb_file_name(file_name: str, num: str, size_tag: str) -> str:
    """`VideoThumbs::getThumbName()` for a version greater than 5.5.2."""
    return f"{file_name}-thumbnail-{num}-{size_tag}.{THUMB_FORMAT}"


def thumb_times(duration: int, count: int = NUM_THUMBS) -> list[int]:
    """When to grab each thumbnail — the same quotient the app uses.

    ClipBucket computes `(int)($duration / $max_num) * $i`, so the frames sit at
    a fixed fraction through the film; reproducing the arithmetic here is what
    makes an imported video's scrub bar identical to an uploaded one's.
    """
    count = max(1, min(count, max(1, duration)))
    quotient = int(duration / count)
    return [quotient * i for i in range(1, count + 1)]


def thumb_subdir(file_name: str) -> str:
    """The thumbnails' directory, relative to the container's `files/thumbs/video`."""
    return f"{MEDIA_DIR}/{file_name}"


def sql_quote(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "''")


# ── the library ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Item:
    """One video the catalogue should hold."""

    kind: str  # 'movie' | 'episode'
    source: str  # absolute path to the media file (opened read-only)
    rel: str  # path relative to the media root, for the description
    category: str  # 'Movies' | 'TV Shows'
    title: str
    tag: str = ""  # the show; empty for a film
    season: int = 0
    episode: int = 0

    @property
    def base(self) -> str:
        """The readable half of `file_name`, at most `BASE_LENGTH` characters.

        For an episode the season/episode numbers are sized first and the show
        is trimmed around them: cutting the whole string instead would turn
        `S11E115` into `S11E11`, so two episodes of one show would differ only
        by their digest — a name nobody can read at a glance.
        """
        if self.kind != "episode":
            return slugify(self.title)[:BASE_LENGTH]
        numbers = f"s{self.season:02d}e{self.episode:02d}"
        room = max(4, BASE_LENGTH - len(numbers) - 1)
        show = slugify(self.tag)[:room].strip("-")
        return f"{show}-{numbers}"

    @property
    def file_name(self) -> str:
        return file_name_for(self.rel, self.base)

    @property
    def description(self) -> str:
        if self.kind == "episode":
            head = f"{self.tag} — season {self.season}, episode {self.episode}."
        else:
            head = f"{self.title}."
        return f"{head}\n\nFrom the media library ({self.rel})."


def is_video(name: str) -> bool:
    """A media file, not a sidecar and not a partial download."""
    return not name.startswith(".") and name.lower().endswith(VIDEO_SUFFIXES)


def scan_library(
    media_root: str,
    movies_subdir: str = MOVIES_SUBDIR_DEFAULT,
    tv_subdir: str = TV_SUBDIR_DEFAULT,
) -> tuple[list[Item], list[str]]:
    """Walk the media library. Returns (items, unrecognised video files).

    The **folder** is the show or the film — that is how the library is
    organised and how Jellyfin reads it — so the folder name wins over anything
    reconstructed from a file name.
    """
    items: list[Item] = []
    unparsed: list[str] = []

    movies_root = os.path.join(media_root, movies_subdir)
    if os.path.isdir(movies_root):
        for folder in sorted(os.listdir(movies_root)):
            folder_path = os.path.join(movies_root, folder)
            if not os.path.isdir(folder_path) or folder.startswith("."):
                continue
            files = sorted(f for f in os.listdir(folder_path) if is_video(f))
            if not files:
                continue
            # One film per folder: a second file is another copy, not another
            # film, and the largest is how a human reading the folder sees it.
            biggest = max(files, key=lambda f: os.path.getsize(os.path.join(folder_path, f)))
            source = os.path.join(folder_path, biggest)
            items.append(
                Item(
                    kind="movie",
                    source=source,
                    rel=os.path.relpath(source, media_root),
                    category="Movies",
                    title=fit_title(folder),
                )
            )

    tv_root = os.path.join(media_root, tv_subdir)
    if os.path.isdir(tv_root):
        for folder in sorted(os.listdir(tv_root)):
            folder_path = os.path.join(tv_root, folder)
            if not os.path.isdir(folder_path) or folder.startswith("."):
                continue
            for name in sorted(os.listdir(folder_path)):
                path = os.path.join(folder_path, name)
                if not os.path.isfile(path) or not is_video(name):
                    continue
                parsed = parse_episode_name(os.path.splitext(name)[0])
                if not parsed:
                    unparsed.append(path)
                    continue
                _show, season, episode, episode_title = parsed
                items.append(
                    Item(
                        kind="episode",
                        source=path,
                        rel=os.path.relpath(path, media_root),
                        category="TV Shows",
                        title=render_title("episode", folder, season, episode, episode_title),
                        tag=folder,
                        season=season,
                        episode=episode,
                    )
                )

    return items, unparsed


# ── facts about a deployment, and the verdict over them ─────────────────────


def probe(path: str) -> dict:
    """What the catalogue needs to know about a file: codecs, size, length."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_entries", "format=duration,format_name",
            "-show_entries", "stream=index,codec_type,codec_name,width,height",
            path,
        ],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise ImportError_(f"ffprobe could not read {path}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "duration": int(float(data.get("format", {}).get("duration") or 0)),
        # ffprobe's own string, kept whole: it is a list of candidate formats and
        # `media_decision` reads all of them.
        "container": data.get("format", {}).get("format_name") or "",
        "video_codec": (video or {}).get("codec_name", ""),
        "audio_codec": (audio or {}).get("codec_name", ""),
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
    }


def evaluate(facts: dict) -> None:
    """The preconditions for judging a deployment, from facts already read.

    Pure so both directions are unit-testable; raises `CantTell` with the
    reason when nothing can be judged. Note that this is deliberately *not* a
    list of findings: "no docker here" is not a drifted host.
    """
    if not facts.get("docker"):
        raise CantTell("docker is not available here")
    if not facts.get("container_running"):
        raise CantTell("the clipbucket container is not running")
    if not facts.get("ffmpeg"):
        raise CantTell("ffmpeg is not on PATH")
    if not facts.get("ffprobe"):
        raise CantTell("ffprobe is not on PATH")
    if not facts.get("files_path"):
        raise CantTell("the container's file volume could not be located on this host")
    if not facts.get("media_root_is_dir"):
        raise CantTell(f"the media root {facts.get('media_root')!r} is not a directory")
    if facts.get("install_version") != CORE_VERSION:
        raise CantTell(
            f"the install reports version {facts.get('install_version')!r}, not "
            f"{CORE_VERSION!r} — re-read this script's constants against the release"
        )


# ── the site actually serving what the catalogue holds ──────────────────────
#
# Everything above judges this tool's *model* of the app: the file name
# `get_video_files()` builds, the `video_files` JSON, the thumbnail layout. A
# model can be wrong in a way nothing above notices — the row exists, the file
# exists, and the watch page comes back with no playable source because the app
# builds a different name. That is not hypothetical: the first pass wrote
# `<name>-<hash>.mp4` and produced rows whose pages had no `<source>` at all.
#
# So `--serve-check` asks the app instead of the model. It fetches each item's
# watch page from inside the container, takes the sources the page emits, and
# requires (a) at least one, (b) a file on disk behind each, and (c) bytes from
# the web server for it. (c) is the half no filesystem check can see: a file
# the container user cannot read is listed by `ls` and answered with a 403.
SERVE_MARKER = "/files/"
SOURCE_RE = re.compile(r"<source\b[^>]*\bsrc=['\"]([^'\"]+)['\"]", re.IGNORECASE)
WATCH_URL_DEFAULT = "http://127.0.0.1/watch_video.php?v={videoid}"


def page_sources(body: str) -> list[str]:
    """The `src` of every `<source>` the watch page emits, in page order."""
    return SOURCE_RE.findall(body)


def source_file(url: str, files_path: str) -> str:
    """Where a page's `/files/...` source lives on this host, or "".

    The web root is `<volume>/upload`, so a URL path of `/files/videos/…` is
    `<files_path>/upload/files/videos/…` on disk — the same arithmetic the
    apply uses when it writes the file in the first place.
    """
    idx = url.find(SERVE_MARKER)
    if idx < 0:
        return ""
    return os.path.join(files_path, "upload", url[idx + 1:].lstrip("/"))


def _curl_in(container: str, url: str, write_out: str = "") -> subprocess.CompletedProcess:
    """curl the site from inside the container.

    Inside, because the page's own URLs carry a public hostname that may not
    resolve from here (and must not be needed to judge a deployment), while
    127.0.0.1:80 is the same site the caller reaches.
    """
    args = ["docker", "exec", container, "curl", "-sS", "-m", "30", "-o", write_out or "/dev/null"]
    return run(args + [url], timeout=120)


def watch_page(container: str, videoid: int) -> tuple[int, str]:
    """(HTTP status, body) for a watch page, fetched inside the container."""
    proc = run(
        [
            "docker", "exec", container, "curl", "-sS", "-m", "30",
            "-w", "\n%{http_code}",
            WATCH_URL_DEFAULT.format(videoid=videoid),
        ],
        timeout=120,
    )
    if proc.returncode != 0:
        raise ImportError_(
            f"could not fetch the watch page for v={videoid} in {container}: "
            f"{proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else proc.returncode}"
        )
    body, _, code = proc.stdout.rpartition("\n")
    try:
        return int(code.strip()), body
    except ValueError:
        return 0, body


def serves_bytes(container: str, url: str) -> int:
    """The status the site answers a 1 KiB range of `url` with, or 0."""
    idx = url.find(SERVE_MARKER)
    if idx < 0:
        return 0
    proc = run(
        [
            "docker", "exec", container, "curl", "-sS", "-m", "30",
            "-o", "/dev/null", "-w", "%{http_code}", "-r", "0-1023",
            f"http://127.0.0.1{url[idx:]}",
        ],
        timeout=120,
    )
    try:
        return int(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def serve_report(items: list[Item], facts: dict, ids: dict[str, int]) -> list[tuple[str, str]]:
    """(file_name, why) for every item whose watch page does not serve a file."""
    problems: list[tuple[str, str]] = []
    for item in items:
        videoid = ids.get(item.file_name)
        if not videoid:
            problems.append((item.file_name, "no cb_video row, so there is no watch page"))
            continue
        try:
            status, body = watch_page(facts["container"], videoid)
        except ImportError_ as exc:
            problems.append((item.file_name, str(exc)))
            continue
        if status != 200:
            problems.append(
                (item.file_name, f"watch_video.php?v={videoid} answered HTTP {status}")
            )
            continue
        urls = page_sources(body)
        if not urls:
            problems.append(
                (
                    item.file_name,
                    f"the watch page (v={videoid}) emits no <source> — the app "
                    "built no playable file for this row",
                )
            )
            continue
        bad = []
        for url in urls:
            path = source_file(url, facts["files_path"])
            if not path:
                bad.append(f"{url} is not a {SERVE_MARKER} URL")
            elif not os.path.isfile(path):
                bad.append(f"{url} has no file at {path}")
            else:
                code = serves_bytes(facts["container"], url)
                if code not in (200, 206):
                    bad.append(f"{url} answered HTTP {code} to a range request")
        if bad:
            problems.append((item.file_name, "; ".join(bad)))
    return problems


def evaluate_serve(facts: dict) -> None:
    """Preconditions for judging what the *site* serves.

    Deliberately not `evaluate`: serving an item needs no ffmpeg and no media
    root, and refusing to judge a media host because it has no encoder on PATH
    would turn a working deployment into a "cannot tell".
    """
    if not facts.get("docker"):
        raise CantTell("docker is not available here")
    if not facts.get("container_running"):
        raise CantTell("the clipbucket container is not running")
    if not facts.get("files_path"):
        raise CantTell("the container's file volume could not be located on this host")
    if not facts.get("media_root_is_dir"):
        raise CantTell(f"the media root {facts.get('media_root')!r} is not a directory")
    if facts.get("install_version") != CORE_VERSION:
        raise CantTell(
            f"the install reports version {facts.get('install_version')!r}, not "
            f"{CORE_VERSION!r} — re-read this script's constants against the release"
        )


# ── docker, ffmpeg and mysql ────────────────────────────────────────────────


def run(args: list[str], stdin: str | None = None, timeout: int = 7200) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, input=stdin, timeout=timeout)


def container_running(name: str) -> bool:
    proc = run(["docker", "inspect", "-f", "{{.State.Running}}", name], timeout=60)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def resolve_container(explicit: str, candidates: tuple[str, ...] = (CONTAINER_DEFAULT,)) -> str:
    """The container to write to, or "".

    An explicit name is not a candidate among the defaults: naming a container
    that is not running has to surface as "no ClipBucket", never as a
    neighbouring stack's container.
    """
    if explicit:
        return explicit if container_running(explicit) else ""
    for name in candidates:
        if container_running(name):
            return name
    return ""


def mount_source(container: str, destination: str) -> str:
    """The host path behind a container path (a named volume's mountpoint)."""
    proc = run(
        ["docker", "inspect", "-f",
         '{{range .Mounts}}{{.Source}}=>{{.Destination}}{{"\\n"}}{{end}}', container],
        timeout=60,
    )
    if proc.returncode != 0:
        return ""
    for line in proc.stdout.splitlines():
        source, _, dest = line.partition("=>")
        if dest.strip() == destination:
            return source.strip()
    return ""


def mysql_query(container: str, sql: str) -> list[list[str]]:
    """Run a statement and return its tab-separated rows."""
    proc = run(
        ["docker", "exec", container, "mysql", "-N", "-B", "-u", DB_USER, DB_NAME, "-e", sql],
        timeout=300,
    )
    if proc.returncode != 0:
        raise ImportError_(f"mysql rejected `{sql}`: {proc.stderr.strip()}")
    return [line.split("\t") for line in proc.stdout.splitlines() if line.strip()]


def mysql_exec(container: str, sql: str) -> None:
    """Run statements for their effect (several per call is fine)."""
    proc = run(
        ["docker", "exec", container, "mysql", "-N", "-B", "-u", DB_USER, DB_NAME, "-e", sql],
        timeout=300,
    )
    if proc.returncode != 0:
        raise ImportError_(f"mysql rejected `{sql}`: {proc.stderr.strip()}")


def mysql_insert_id(container: str, sql: str) -> int:
    """Run one INSERT and return its AUTO_INCREMENT id, from the same session."""
    rows = mysql_query(container, f"{sql} SELECT LAST_INSERT_ID();")
    if not rows:
        raise ImportError_(f"mysql returned no id for `{sql}`")
    return int(rows[-1][0])


def ensure_category(container: str, name: str) -> int:
    """The video category, created once."""
    rows = mysql_query(
        container,
        "SELECT category_id FROM cb_categories WHERE "
        f"id_category_type={VIDEO_CATEGORY_TYPE} AND category_name='{sql_quote(name)}';",
    )
    if rows:
        return int(rows[0][0])
    return mysql_insert_id(
        container,
        "INSERT INTO cb_categories (parent_id, id_category_type, category_name, "
        f"category_order, date_added, is_default) VALUES (NULL, {VIDEO_CATEGORY_TYPE}, "
        f"'{sql_quote(name)}', 0, NOW(), NULL);",
    )


def ensure_tag(container: str, name: str) -> int:
    """A video tag, created once."""
    rows = mysql_query(
        container,
        f"SELECT id_tag FROM cb_tags WHERE id_tag_type={VIDEO_TAG_TYPE} AND name='{sql_quote(name)}';",
    )
    if rows:
        return int(rows[0][0])
    return mysql_insert_id(
        container,
        f"INSERT INTO cb_tags (id_tag_type, name) VALUES ({VIDEO_TAG_TYPE}, '{sql_quote(name)}');",
    )


# ── collections: a series is a set worth moving through ─────────────────────


def series_of(items: list[Item]) -> dict[str, list[Item]]:
    """Show -> its episodes, in season/episode order.

    Only episodes. A show's episodes are one set a viewer moves through; a film
    is already the unit the Movies category lists, and a one-item collection per
    film would be a second way of saying the same thing.
    """
    groups: dict[str, list[Item]] = {}
    for item in items:
        if item.kind == "episode":
            groups.setdefault(item.tag, []).append(item)
    for episodes in groups.values():
        episodes.sort(key=lambda i: (i.season, i.episode))
    return groups


def collection_description(name: str, count: int) -> str:
    return f"Every episode of {name} in the library ({count})."


def collection_insert_sql(name: str, description: str, created: str, thumb_videoid: int) -> str:
    """The INSERT for a collection that is not there yet.

    Every column the table requires without a default is named: `broadcast`,
    `active` and `public_upload` are NOT NULL with none, and a row inserted
    without them is either refused or, worse, lands inactive and drops off
    `collections.php` while its items keep pointing at it.
    """
    return (
        "INSERT INTO cb_collections (collection_name, collection_description, userid, "
        "date_added, featured, hierarchy_featured, broadcast, allow_comments, "
        "allow_rating, total_comments, active, public_upload, type, thumb_objectid, "
        "total_rate_up, total_rate_down) VALUES ("
        f"'{sql_quote(name)}', '{sql_quote(description)}', {UPLOADER_USERID}, "
        f"'{created}', 'no', 'no', 'public', 'yes', 'yes', 0, 'yes', 'no', 'videos', "
        f"{thumb_videoid}, 0, 0);"
    )


def ensure_collection(
    container: str, name: str, description: str, created: str, thumb_videoid: int
) -> int:
    """The series collection, created once and kept visible."""
    rows = mysql_query(
        container,
        "SELECT collection_id FROM cb_collections WHERE "
        f"collection_name='{sql_quote(name)}' AND type='videos' LIMIT 1;",
    )
    if not rows:
        return mysql_insert_id(container, collection_insert_sql(name, description, created, thumb_videoid))
    cid = int(rows[0][0])
    # An existing row is re-asserted rather than trusted: a collection someone
    # switched off in the admin area is invisible, and its episodes would look
    # imported while zero of them could be reached.
    mysql_exec(
        container,
        "UPDATE cb_collections SET active='yes', broadcast='public', "
        f"collection_description='{sql_quote(description)}', "
        f"thumb_objectid={thumb_videoid} WHERE collection_id={cid};",
    )
    return cid


def sync_collection(
    container: str, name: str, videoids: list[int], category_id: int, created: str
) -> tuple[int, str]:
    """Make one series collection hold exactly `videoids`. Returns (id, note).

    Converged, not appended. `cb_collection_items` has no unique key on
    (collection_id, object_id), so a second INSERT of the same episode is a
    duplicate row the app cannot tell from a real second copy — which is what a
    re-run would leave behind without the read below.
    """
    if not videoids:
        return 0, "no episodes"
    cid = ensure_collection(
        container, name, collection_description(name, len(videoids)), created, videoids[0]
    )
    # A collection is a category's worth of videos too, so the collection page
    # can list it under the same heading as the episodes it holds.
    mysql_exec(
        container,
        "INSERT IGNORE INTO cb_collections_categories (id_collection, id_category) "
        f"VALUES ({cid}, {category_id});",
    )

    rows = mysql_query(
        container,
        f"SELECT object_id FROM cb_collection_items WHERE collection_id={cid} AND type='videos';",
    )
    present = {int(row[0]) for row in rows}
    wanted = set(videoids)
    extra = present - wanted
    if extra:
        mysql_exec(
            container,
            f"DELETE FROM cb_collection_items WHERE collection_id={cid} AND object_id IN "
            f"({', '.join(str(v) for v in sorted(extra))});",
        )
    added = [v for v in videoids if v not in present]
    if added:
        # One statement, in viewing order: the rows carry no sort column, so the
        # insertion order *is* the order the collection lists its episodes in.
        values = ", ".join(
            f"({cid}, {v}, {UPLOADER_USERID}, 'videos', '{created}')" for v in added
        )
        mysql_exec(
            container,
            "INSERT INTO cb_collection_items (collection_id, object_id, userid, type, "
            f"date_added) VALUES {values};",
        )
    if not added and not extra:
        return cid, f"collection {cid}: in sync ({len(wanted)} episode(s))"
    return cid, f"collection {cid}: +{len(added)} episode(s), -{len(extra)}"


def collection_state(container: str, names: list[str]) -> dict[str, tuple[int, set[int]]]:
    """collection_name -> (collection_id, member videoids), read once."""
    if not names:
        return {}
    listed = ", ".join(f"'{sql_quote(n)}'" for n in names)
    rows = mysql_query(
        container,
        "SELECT collection_name, collection_id FROM cb_collections WHERE "
        f"type='videos' AND active='yes' AND collection_name IN ({listed});",
    )
    ids = {row[0]: int(row[1]) for row in rows if len(row) >= 2}
    state: dict[str, tuple[int, set[int]]] = {}
    if ids:
        members = mysql_query(
            container,
            f"SELECT collection_id, object_id FROM cb_collection_items WHERE "
            f"type='videos' AND collection_id IN ({', '.join(str(i) for i in ids.values())});",
        )
        by_id: dict[int, set[int]] = {i: set() for i in ids.values()}
        for row in members:
            if len(row) >= 2:
                by_id.setdefault(int(row[0]), set()).add(int(row[1]))
        state = {name: (cid, by_id.get(cid, set())) for name, cid in ids.items()}
    return state


def missing_collections(series: dict[str, list[Item]], ids: dict[str, int], facts: dict) -> list[str]:
    """Shows whose collection is absent, or does not hold exactly their episodes.

    A show whose episodes have no rows yet is skipped: those are already the
    loudest finding in the report, and repeating them here would say the same
    thing twice.
    """
    state = collection_state(facts["container"], list(series)) if series else {}
    problems: list[str] = []
    for name, episodes in series.items():
        wanted = {ids[i.file_name] for i in episodes if i.file_name in ids}
        if len(wanted) != len(episodes):
            continue
        found = state.get(name)
        if found is None or found[1] != wanted:
            problems.append(name)
    return problems


# ── the check ───────────────────────────────────────────────────────────────


@dataclass
class Report:
    ok: list[str]
    missing_rows: list[str]
    missing_media: list[str]
    missing_thumbs: list[str]
    missing_collections: list[str]
    unparsed: list[str]
    # Present and playable, but not what a stream copy of the source produces:
    # a copy an earlier version of this tool re-encoded. Its own category
    # because "the file is missing" is a different repair from "the file is
    # here and the wrong codec", and because a check that stayed silent about
    # it would call a host in sync while `--apply` rewrote its media.
    converted: list[str]

    @property
    def drift(self) -> bool:
        return bool(
            self.missing_rows or self.missing_media or self.missing_thumbs
            or self.missing_collections or self.converted
        )


def missing_thumbs(thumb_dir: str, file_name: str, num_thumbs: int) -> list[str]:
    """Which of the app's thumbnails are not there.

    The files live one directory down, named after the video
    (`thumbs/video/<dir>/<file_name>/`), because `VideoThumbs` writes them
    there from 5.5.3/14 on — a flat `thumb_dir/name` check reports all 25 as
    missing while they are all present.
    """
    wanted = []
    for i in range(1, num_thumbs + 1):
        for size_tag, _w, _h in THUMB_RESOLUTIONS:
            name = thumb_file_name(file_name, thumb_num(i), size_tag)
            if not os.path.exists(os.path.join(thumb_dir, file_name, name)):
                wanted.append(name)
    return wanted


def media_is_faithful(item: Item, videos: str) -> str:
    """Why this item's catalogue file is not a stream copy of its source.

    Empty when it is one. This is the read-only half of the rule the apply path
    enforces through `copy_is_reusable`: without it `--check` reports `in sync`
    for a host whose every file `--apply` would replace, which is the one thing
    a drift check must never do. Judged by codec rather than duration because
    the copy exists at all here, and a converted file has the right length.

    A source that cannot be probed is left unsaid rather than guessed at — the
    apply will refuse it on its own terms.
    """
    app_named = [n for n in media_files(videos, item.file_name) if MEDIA_NAME_RE.match(n)]
    try:
        source = probe(item.source)
    except (ImportError_, OSError):
        return ""
    want = expected_codec(
        source["video_codec"],
        media_decision(source["video_codec"], source["audio_codec"], source["container"]),
    )
    for name in app_named:
        try:
            found = probe(os.path.join(videos, name))["video_codec"]
        except (ImportError_, OSError):
            continue
        if found != want:
            return (
                f"its catalogue file holds {found} where a stream copy of the "
                f"source is {want}"
            )
    return ""


def catalogue_state(items: list[Item], facts: dict) -> tuple[dict[str, int], dict[str, int]]:
    """(videoid by file_name, duration by file_name) for the items, read once."""
    names = ", ".join(f"'{sql_quote(i.file_name)}'" for i in items)
    rows = mysql_query(
        facts["container"],
        f"SELECT file_name, videoid, duration FROM cb_video WHERE file_name IN ({names});",
    )
    ids = {row[0]: int(row[1]) for row in rows if len(row) >= 2}
    durations = {row[0]: int(row[2]) for row in rows if len(row) >= 3}
    return ids, durations


def build_report(items: list[Item], unparsed: list[str], facts: dict) -> Report:
    """Compare the library against the catalogue. Read-only."""
    files_path = facts["files_path"]
    videos = os.path.join(files_path, "upload", "files", "videos", MEDIA_DIR)
    thumbs = os.path.join(files_path, "upload", "files", "thumbs", "video", MEDIA_DIR)
    ids, durations = catalogue_state(items, facts)
    series = series_of(items)

    ok: list[str] = []
    missing_rows: list[str] = []
    missing_media: list[str] = []
    missing_thumbs_: list[str] = []
    converted: list[str] = []
    for item in items:
        name = item.file_name
        if name not in ids:
            missing_rows.append(name)
            continue
        problems = False
        if not named_for_the_app(media_files(videos, name)):
            missing_media.append(name)
            problems = True
        elif (why := media_is_faithful(item, videos)):
            # Only when the file is there and named as the app expects: over a
            # missing file this is the same finding twice.
            converted.append(f"{name}: {why}")
            problems = True
        # A row without its thumbnails renders a broken card, which is why this
        # is drift and not a cosmetic note.
        if missing_thumbs(thumbs, name, len(thumb_times(durations.get(name, 0)))):
            missing_thumbs_.append(name)
            problems = True
        if not problems:
            ok.append(name)

    return Report(
        ok=ok,
        missing_rows=missing_rows,
        missing_media=missing_media,
        missing_thumbs=missing_thumbs_,
        missing_collections=missing_collections(series, ids, facts),
        unparsed=unparsed,
        converted=converted,
    )


def describe(report: Report, stream) -> None:
    for name in report.ok:
        print(f"  ok           {name}", file=stream)
    for name in report.missing_rows:
        print(f"  missing      {name}: no cb_video row — the library item is invisible", file=stream)
    for name in report.missing_media:
        print(f"  incomplete   {name}: the row exists and its media file does not", file=stream)
    for entry in report.converted:
        print(
            f"  converted    {entry} — re-run --apply to re-copy it from the "
            f"source (a stream copy, seconds, no re-encode)",
            file=stream,
        )
    for name in report.missing_thumbs:
        print(f"  incomplete   {name}: thumbnails are missing — the card renders broken", file=stream)
    for name in report.missing_collections:
        print(f"  incomplete   {name}: no series collection, or it does not hold its episodes", file=stream)
    for path in report.unparsed:
        print(f"  skipped      {path}: no season/episode in the name, so not guessed at", file=stream)


def hevc_line(count: int) -> str:
    """The recorded limitation, phrased once for every place it is reported.

    HEVC is *kept* rather than converted, so this is not a note about work left
    undone — it is the number of items whose playability depends on the client
    having an HEVC decoder (Safari, Edge, and Chrome on hardware that decodes
    it; Firefox has none). One function so `--check` and `--apply` cannot
    describe the same count two different ways.
    """
    return (
        f"clipbucket-library: {count} item(s) carry HEVC — a stream copy of it plays "
        "where the client decodes HEVC (Safari, Edge, HEVC-capable Chrome) and not "
        "in Firefox. Nothing here re-encodes; the source codec is kept"
    )


def hevc_sources(items: list[Item]) -> int:
    """How many items' sources are HEVC. Read-only, one ffprobe each.

    Judged from the source rather than the catalogue copy, so the number is a
    property of the library and not of whatever the last run happened to write.
    """
    count = 0
    for item in items:
        try:
            if probe(item.source)["video_codec"] == HEVC:
                count += 1
        except (ImportError_, OSError):
            # Unreadable is not a claim that it is H.264, and a source this tool
            # cannot read is already a finding of its own.
            continue
    return count


# ── the apply ───────────────────────────────────────────────────────────────


def media_is_complete(dest: str, expected_duration: int) -> bool:
    """A finished file, not a truncated one.

    An interrupted import — a killed process, a reboot, a Ctrl-C — leaves the
    part-written MP4 behind, and "the file exists and is not empty" would call
    that done: the catalogue would then hold a video that stops in the middle,
    with nothing to distinguish it from one that plays. Because the conversion
    is a copy, the file's own duration has to agree with the source's; that is
    the cheapest evidence that it was written to the end.
    """
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        return False
    try:
        return abs(probe(dest)["duration"] - expected_duration) <= 2
    except ImportError_:
        return False


def copy_is_reusable(path: str, expected_duration: int, want_codec: str) -> bool:
    """Whether a file already in place can be left where it is.

    Two questions, and the second is the one this tool learned the hard way: is
    it written to the end, and does it carry the codec a stream copy of the
    source would? Duration alone called a transcoded file "done" — it has the
    right length — so the codec is what separates *converted* from *copied*, and
    an item that was converted to H.264 in an earlier version of this tool
    reads as drift now and is replaced by a stream copy. That is the repair, and
    it costs seconds rather than the hours the conversion cost.
    """
    if not media_is_complete(path, expected_duration):
        return False
    try:
        return probe(path)["video_codec"] == want_codec
    except ImportError_:
        # Unreadable is not a reason to call it good, and the rebuild below
        # overwrites it anyway.
        return False


def replacement_reason(
    path: str, expected_duration: int, decision: str, source_codec: str
) -> str:
    """Why a complete-but-unusable file is being replaced.

    "Replaced an incomplete file" over a file that was written to the end reads
    as data loss, so the cases are named separately — and the converted one
    names both codecs, because "it was converted" is the fact that explains why
    a repair is happening at all.
    """
    if not media_is_complete(path, expected_duration):
        return "replaced an incomplete file"
    if decision == "transcode":
        return "replaced a file that is not a web-playable copy"
    try:
        found = probe(path)["video_codec"]
    except ImportError_:
        return "replaced an unreadable copy"
    if found != source_codec:
        return (
            f"replaced a converted ({found}) copy with a stream copy of the "
            f"{source_codec} source"
        )
    return f"replaced a {found} copy with a stream copy of the source"


def materialise_media(item: Item, source_facts: dict, videos_dir: str) -> tuple[str, int]:
    """Put a playable MP4 in `videos_dir`. Returns (what it had to do, quality)."""
    quality = quality_for(source_facts["width"], source_facts["height"])
    dest = os.path.join(videos_dir, media_name(item.file_name, quality))
    os.makedirs(videos_dir, exist_ok=True)
    # Decided from the source alone, before anything is looked at, so the reuse
    # rule and the rebuild agree about what a faithful copy is.
    decision = media_decision(
        source_facts["video_codec"], source_facts["audio_codec"], source_facts["container"],
    )
    want_codec = expected_codec(source_facts["video_codec"], decision)
    prefix = ""
    if copy_is_reusable(dest, source_facts["duration"], want_codec):
        return "already there", quality
    if os.path.exists(dest):
        # Only ever this tool's own artifact, under its own name, and only
        # because it is provably not the video: a usable file never reaches
        # here.
        prefix = (
            f"{replacement_reason(dest, source_facts['duration'], decision, source_facts['video_codec'])}, "
        )
        os.remove(dest)

    stale = stale_media(videos_dir, item.file_name, media_name(item.file_name, quality))
    if len(stale) == 1 and copy_is_reusable(
        os.path.join(videos_dir, stale[0]), source_facts["duration"], want_codec
    ):
        # One earlier attempt under the previous name, written to the end and
        # carrying the right codec: the bytes are right, only the label the app
        # reads the resolution from was wrong, so renaming repairs it. A partial
        # one, or a converted one, falls through to the rebuild below.
        os.rename(os.path.join(videos_dir, stale[0]), dest)
        return f"{prefix}adopted {stale[0]}", quality
    for name in stale:
        os.remove(os.path.join(videos_dir, name))

    if decision == "link":
        try:
            # The media root and the docker volumes share a filesystem here, so
            # this costs no space and no time. It is also why the source must
            # never be written to: from here it has two names.
            os.link(item.source, dest)
            return f"{prefix}hardlinked (already H.264/AAC MP4)", quality
        except OSError:
            decision = "remux"  # a copy is the honest fallback: still no re-encode
    proc = run(media_argv(item.source, dest, decision, source_facts["video_codec"]))
    if proc.returncode != 0 or not os.path.exists(dest):
        raise ImportError_(f"ffmpeg failed for {item.source}: {proc.stderr.strip()}")
    if not media_is_complete(dest, source_facts["duration"]):
        raise ImportError_(
            f"ffmpeg stopped short on {item.source} — {dest} is not the whole video"
        )
    if decision == "transcode":
        return f"{prefix}re-encoded to H.264 (no browser decodes {source_facts['video_codec']})", quality
    return f"{prefix}remuxed (video copied, audio to stereo AAC)", quality


def write_thumbs(item: Item, source_facts: dict, thumb_dir: str) -> int:
    """Generate the app's thumbnails for one video. Returns how many were written."""
    os.makedirs(thumb_dir, exist_ok=True)
    written = 0
    for i, at in enumerate(thumb_times(source_facts["duration"]), start=1):
        num = thumb_num(i)
        for size_tag, width, height in THUMB_RESOLUTIONS:
            dest = os.path.join(thumb_dir, thumb_file_name(item.file_name, num, size_tag))
            if os.path.exists(dest):
                continue
            proc = run(thumb_argv(item.source, dest, at, size_tag, width, height))
            if proc.returncode != 0 or not os.path.exists(dest):
                raise ImportError_(f"ffmpeg could not make {dest}: {proc.stderr.strip()}")
            written += 1
    return written


def created_at(item: Item) -> str:
    """When the item landed, taken from the file rather than from this run.

    `date_added` is a DEFAULT CURRENT_TIMESTAMP column; letting it stand would
    make the catalogue's history the import's run time, which is the opposite of
    what it is for.
    """
    return datetime.datetime.fromtimestamp(os.path.getmtime(item.source)).strftime("%Y-%m-%d %H:%M:%S")


def row_sql(item: Item, source_facts: dict, quality: int) -> str:
    """The UPDATE that keeps an existing row in the state the browse query wants."""
    created = created_at(item)
    aspect = (
        round(source_facts["width"] / source_facts["height"], 6) if source_facts["height"] else None
    )
    values: dict[str, object] = {
        "username": UPLOADER_NAME,
        "userid": UPLOADER_USERID,
        "title": item.title,
        "file_type": "mp4",
        "file_directory": MEDIA_DIR,
        # The one file this item has, in the app's own vocabulary: the quality
        # is both the file's label and how `get_video_files` finds it.
        "video_files": json.dumps([quality]),
        "description": item.description,
        "broadcast": "public",
        "datecreated": created,
        "date_added": created,
        "allow_embedding": "yes",
        "allow_comments": "yes",
        "comment_voting": "yes",
        "featured": "no",
        "allow_rating": "yes",
        "active": "yes",
        "status": "Successful",
        "duration": source_facts["duration"],
        "aspect_ratio": aspect,
        "is_castable": 1,
        "uploader_ip": "127.0.0.1",
        "video_version": CORE_VERSION,
        "subscription_email": "pending",
        "flagged": "no",
        "last_modified": created,
    }
    fields = []
    for field, value in values.items():
        if value is None:
            fields.append(f"`{field}`=NULL")
        elif isinstance(value, int):
            fields.append(f"`{field}`={value}")
        elif isinstance(value, float):
            fields.append(f"`{field}`={value:.6f}")
        else:
            fields.append(f"`{field}`='{sql_quote(value)}'")
    return f"UPDATE cb_video SET {', '.join(fields)} WHERE file_name='{sql_quote(item.file_name)}';"


def insert_sql(item: Item, source_facts: dict, quality: int) -> str:
    """The INSERT for an item with no row yet."""
    created = created_at(item)
    aspect = (
        round(source_facts["width"] / source_facts["height"], 6) if source_facts["height"] else None
    )
    return (
        "INSERT INTO cb_video (videokey, video_password, username, userid, title, "
        "file_name, file_type, file_directory, description, broadcast, datecreated, "
        "date_added, allow_embedding, allow_comments, comment_voting, comments_count, "
        "featured, allow_rating, active, status, duration, aspect_ratio, video_files, "
        "is_castable, uploader_ip, video_version, subscription_email, flagged, "
        "last_modified) VALUES ("
        f"'{secrets.token_hex(5)}', '', '{UPLOADER_NAME}', {UPLOADER_USERID}, "
        f"'{sql_quote(item.title)}', '{sql_quote(item.file_name)}', 'mp4', '{MEDIA_DIR}', "
        f"'{sql_quote(item.description)}', 'public', '{created}', '{created}', 'yes', 'yes', "
        f"'yes', 0, 'no', 'yes', 'yes', 'Successful', {source_facts['duration']}, "
        f"{aspect if aspect is not None else 'NULL'}, '{json.dumps([quality])}', 1, "
        f"'127.0.0.1', '{CORE_VERSION}', 'pending', 'no', '{created}');"
    )


def thumbs_sql(videoid: int, item: Item, source_facts: dict) -> str:
    """The cb_video_image / cb_video_thumb rows, rebuilt for one video.

    Rebuilt rather than appended: the files are named by thumbnail number, so a
    second row for the same number would be a duplicate the app cannot
    distinguish, and a re-run must converge on one set.
    """
    statements = [
        "DELETE t FROM cb_video_thumb t JOIN cb_video_image i "
        f"ON i.id_video_image=t.id_video_image WHERE i.videoid={videoid};",
        f"DELETE FROM cb_video_image WHERE videoid={videoid};",
    ]
    nums = [thumb_num(i) for i in range(1, len(thumb_times(source_facts["duration"])) + 1)]
    if not nums:
        return " ".join(statements)
    first_num = DEFAULT_THUMB_NUM if nums else 0
    for num in nums:
        statements.append(
            "INSERT INTO cb_video_image (videoid, type, num, is_auto) VALUES "
            f"({videoid}, 'thumbnail', {int(num)}, 1);"
        )
        statements.append("SET @id_image := LAST_INSERT_ID();")
        for size_tag, width, height in THUMB_RESOLUTIONS:
            w = source_facts["width"] if size_tag == "original" else width
            h = source_facts["height"] if size_tag == "original" else height
            original = 1 if size_tag == "original" else 0
            statements.append(
                "INSERT INTO cb_video_thumb (id_video_image, width, height, extension, "
                f"version, is_original_size) VALUES (@id_image, {int(w or 0)}, {int(h or 0)}, "
                f"'{THUMB_FORMAT}', '{CORE_VERSION}', {original});"
            )
        if int(num) == first_num:
            statements.append("SET @first_image := @id_image;")
    statements.append(
        "UPDATE cb_video SET default_thumbnail=@first_image, default_thumb=1 "
        f"WHERE videoid={videoid};"
    )
    return " ".join(statements)


def apply_item(
    item: Item, source_facts: dict, facts: dict, category_id: int,
    tag_id: int | None,
) -> tuple[str, int]:
    """Bring one item into the catalogue. Returns (a one-line note, videoid)."""
    container = facts["container"]
    files_path = facts["files_path"]
    videos = os.path.join(files_path, "upload", "files", "videos", MEDIA_DIR)
    thumb_dir = os.path.join(files_path, "upload", "files", "thumbs", "video", thumb_subdir(item.file_name))
    name = item.file_name

    note, quality = materialise_media(item, source_facts, videos)
    thumbs = write_thumbs(item, source_facts, thumb_dir)

    rows = mysql_query(container, f"SELECT videoid FROM cb_video WHERE file_name='{sql_quote(name)}';")
    if rows:
        videoid = int(rows[0][0])
        mysql_exec(container, row_sql(item, source_facts, quality))
    else:
        videoid = mysql_insert_id(container, insert_sql(item, source_facts, quality))

    mysql_exec(container, thumbs_sql(videoid, item, source_facts))
    mysql_exec(
        container,
        "INSERT IGNORE INTO cb_videos_categories (id_video, id_category) "
        f"VALUES ({videoid}, {category_id});",
    )
    if tag_id is not None:
        mysql_exec(container, f"INSERT IGNORE INTO cb_video_tags (id_video, id_tag) VALUES ({videoid}, {tag_id});")
    return f"{note}, {thumbs} thumbnail(s), videoid={videoid}", videoid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Put the media library into ClipBucket's catalogue.",
        epilog=(
            "Exit: 0 in sync (or applied) — 1 behind/drift, or a file this tool will "
            "not guess about — 2 cannot tell."
        ),
    )
    parser.add_argument(
        "--media-root",
        default=os.environ.get("CLIPBUCKET_MEDIA_ROOT", MEDIA_ROOT_DEFAULT),
        help=f"the Jellyfin library root (default {MEDIA_ROOT_DEFAULT})",
    )
    parser.add_argument("--movies-dir", default=MOVIES_SUBDIR_DEFAULT, help="movies under the root")
    parser.add_argument("--tv-dir", default=TV_SUBDIR_DEFAULT, help="TV under the root")
    parser.add_argument("--container", default=os.environ.get("CLIPBUCKET_CONTAINER", ""))
    parser.add_argument(
        "--owner", default=os.environ.get("CLIPBUCKET_FILES_OWNER", "1000:1000"),
        help="uid:gid the container's file volume expects",
    )
    parser.add_argument("--only", choices=("movies", "tv"), help="import one half of the library")
    parser.add_argument("--limit", type=int, help="stop after N items (a first look)")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="report drift, write nothing")
    action.add_argument("--apply", action="store_true", help="write the catalogue")
    action.add_argument(
        "--serve-check",
        action="store_true",
        help="fetch every item's watch page and require a playable source it "
             "will actually serve bytes from (write nothing)",
    )
    parser.add_argument("--quiet", action="store_true", help="only the summary line")
    args = parser.parse_args(argv)

    container = resolve_container(args.container or CONTAINER_DEFAULT)
    facts = {
        "container": container,
        "container_running": bool(container),
        "docker": shutil.which("docker") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "media_root": args.media_root,
        "media_root_is_dir": os.path.isdir(args.media_root),
        "files_path": mount_source(container, FILES_VOLUME_DEST) if container else "",
    }
    # The version the *install* claims, not the constant in this file: a
    # mismatch means the app would take a different code path to the rows this
    # script is about to write.
    try:
        facts["install_version"] = mysql_query(container, "SELECT version FROM cb_version LIMIT 1;")[0][0] if container else ""
    except ImportError_ as exc:
        print(f"clipbucket-library: {exc}", file=sys.stderr)
        return 2
    try:
        # `--serve-check` judges what the app serves, which needs no encoder and
        # no media probe — see evaluate_serve.
        (evaluate_serve if args.serve_check else evaluate)(facts)
    except CantTell as exc:
        print(f"clipbucket-library: {exc}", file=sys.stderr)
        return 2

    items, unparsed = scan_library(args.media_root, args.movies_dir, args.tv_dir)
    if args.only == "movies":
        items = [i for i in items if i.kind == "movie"]
    elif args.only == "tv":
        items = [i for i in items if i.kind == "episode"]
    if args.limit:
        items = items[: args.limit]
    if not items:
        print(
            f"clipbucket-library: no movies or TV episodes under {args.media_root} "
            f"({args.movies_dir}/, {args.tv_dir}/) — nothing to import",
            file=sys.stderr,
        )
        return 2

    if args.serve_check:
        # The app's own answer, item by item: the page it serves and the file
        # behind the source on it. This is the check that would have caught the
        # first pass's `<name>-<hash>.mp4` rows, which every filesystem check
        # above called complete.
        ids, _durations = catalogue_state(items, facts)
        problems = serve_report(items, facts, ids)
        out = sys.stderr if args.quiet else sys.stdout
        for name, why in problems:
            print(f"  not served   {name}: {why}", file=out)
        if problems:
            print(
                f"clipbucket-library: {len(problems)} of {len(items)} item(s) do not "
                "serve a playable file — re-run --apply, then check the file "
                "volume's ownership; see docs/operations.md 'ClipBucket'",
                file=sys.stderr,
            )
            return 1
        print(
            f"clipbucket-library: every one of {len(items)} item(s) serves a "
            "playable file"
        )
        return 0

    if args.check:
        out = sys.stderr if args.quiet else sys.stdout
        report = build_report(items, unparsed, facts)
        describe(report, out)
        # Reported on the clean run as well as a drifted one: a limitation that
        # is only mentioned when something is wrong is the kind that gets
        # rediscovered as a surprise.
        hevc = hevc_sources(items)
        if hevc:
            print(hevc_line(hevc), file=out)
        if report.drift or report.unparsed:
            print(
                f"clipbucket-library: {len(report.missing_rows)} item(s) missing from the "
                f"catalogue, {len(report.missing_media) + len(report.missing_thumbs)} incomplete, "
                f"{len(report.converted)} item(s) to re-copy as a stream copy, "
                f"{len(report.missing_collections)} series collection(s) behind, "
                f"{len(report.unparsed)} skipped",
                file=sys.stderr,
            )
            return 1
        print(f"clipbucket-library: in sync ({len(report.ok)} item(s))")
        return 0

    movies_category = ensure_category(container, "Movies")
    tv_category = ensure_category(container, "TV Shows")
    print(f"clipbucket-library: {len(items)} item(s) to converge", file=sys.stderr)
    # Counted from the sources, and always: the run cannot decide to convert
    # them away, so the number is a property of the library rather than of what
    # this run did.
    hevc_remuxed = 0
    videoid_by_file: dict[str, int] = {}
    for n, item in enumerate(items, start=1):
        source_facts = probe(item.source)
        category_id = tv_category if item.kind == "episode" else movies_category
        # A show is a set worth naming; a film's category is already its set.
        tag_id = ensure_tag(container, item.tag) if item.kind == "episode" else None
        note, videoid = apply_item(item, source_facts, facts, category_id, tag_id)
        videoid_by_file[item.file_name] = videoid
        print(f"  [{n}/{len(items)}] {item.title} — {note}", file=sys.stderr)
        if source_facts["video_codec"] == HEVC:
            hevc_remuxed += 1

    # After the items, never before: a collection is a list of videoids, and
    # those only exist once the videos do.
    series = series_of(items)
    if series:
        print(f"clipbucket-library: {len(series)} series collection(s)", file=sys.stderr)
        for name, episodes in series.items():
            videoids = [
                videoid_by_file[i.file_name]
                for i in episodes
                if i.file_name in videoid_by_file
            ]
            if not videoids:
                continue
            _cid, note = sync_collection(container, name, videoids, tv_category, created_at(episodes[0]))
            print(f"  {name} — {note}", file=sys.stderr)

    # The file volume belongs to the container user. A root-owned file is
    # readable but not replaceable, which is how an import becomes a support
    # ticket the first time the app deletes a video.
    for path in (
        os.path.join(facts["files_path"], "upload", "files", "videos", MEDIA_DIR),
        os.path.join(facts["files_path"], "upload", "files", "thumbs", "video", MEDIA_DIR),
    ):
        run(["chown", "-R", args.owner, path], timeout=600)

    rows = mysql_query(
        container, f"SELECT COUNT(*) FROM cb_video WHERE file_directory='{MEDIA_DIR}';"
    )
    print(f"clipbucket-library: catalogue holds {rows[0][0] if rows else '?'} imported video(s)")
    if hevc_remuxed:
        print(hevc_line(hevc_remuxed), file=sys.stderr)
    return 1 if unparsed else 0


if __name__ == "__main__":
    raise SystemExit(main())
