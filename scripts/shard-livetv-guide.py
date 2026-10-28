#!/usr/bin/env python3
"""Build guide.xml out of one grab per site, instead of one grab for all of them.

Why this exists
---------------
The EPG grabber holds every listing it has fetched until it writes the file at the
end, so a run's peak memory is days x channels. This install is ~6,400 channels
across ten sites, and no single run of it has ever finished at more than one day:
three days died against Node's default heap, three days with a 6 GB heap was killed
by the host's own OOM killer, two days at a 4 GB heap died at task 10,622 of 12,764
with full GCs leaving 5.07 GB *live* (real programme data, not garbage), one day at
4 GB died at task 5,311, and one day at 6 GB was the first run to complete. That
one day is the whole horizon a single process can be given here.

Sharding is the way past it, because the peak is per *process*. A grab for one
site holds that site's listings and nothing else, so each part is small and the
horizon is bounded by disk instead of by the heap. The parts are then merged into
the single guide.xml Jellyfin reads, which is what makes the guide two or three
days deep rather than one.

What it does
------------
1. Reads the channel list (`channels.xml`), which has one entry per (site, channel)
   and names the site in its `site` attribute.
2. Writes a channel file per site, in chunks of `--max-channels`, so a site whose
   own listing is large is split further rather than trusted to fit.
3. Runs the grabber once per chunk inside the container, writing one part guide.
   A chunk whose grab runs out of heap is halved and retried (down to
   `MIN_CHANNELS`), because the heap a grab needs is set by the *programmes* its
   channels carry, not by the channel count the chunking was sized against.
4. Merges the parts into `--out`: channels deduplicated by id, programmes by
   (channel, start), in part order so the same input always produces the same file.

The container's own grab is expected to be off (see `init/iptv-pm2.config.js`): two
writers to one guide.xml is not a race worth leaving in place.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CHANNEL_RE = re.compile(r"<channel\b[^>]*>.*?</channel>", re.S)
SITE_RE = re.compile(r'\bsite="([^"]*)"')

# The container mounts the EPG directory at /epg/public, so a host path under the
# EPG dir is a container path under this prefix.
CONTAINER_PUBLIC = "public"

# A grab that runs out of heap is split and retried until its channels are down to
# this many. The variation is real: 900 channels of tvtv.us fit inside this
# container's heap, while 729 of one site did not, and the channel count is not what
# decides it. Halving costs a retry and keeps the site in the guide; dropping the
# part silently costs that site's listings for the whole run.
MIN_CHANNELS = 100

# How the grabber dies when it is out of heap: V8 aborts with SIGABRT (134) and says
# why on stderr, and a grab killed by the container's own memory limit comes back as
# SIGKILL (137). Both are memory, and both get smaller if the part is split.
OOM_MARKERS = ("heap out of memory", "reached heap limit")
OOM_EXIT_CODES = (134, 137)


def parse_channels(text: str) -> "dict[str, list[str]]":
    """Group the channel entries by the site that serves them, in file order.

    Order is kept because the channel list is assembled most-useful-site-first, and
    the merge below keeps the first programme it sees for a slot: two sites covering
    one channel must not fight over it in an order that changes between runs.
    """
    grouped: dict[str, list[str]] = {}
    for match in CHANNEL_RE.finditer(text):
        entry = match.group(0)
        site = SITE_RE.search(entry)
        if not site:
            continue
        grouped.setdefault(site.group(1), []).append(entry)
    return grouped


def chunk(entries: "list[str]", size: int) -> "list[list[str]]":
    """Split one site's entries into pieces of at most `size`."""
    if size <= 0:
        return [entries]
    return [entries[i:i + size] for i in range(0, len(entries), size)] or [entries]


def write_channel_list(path: Path, entries: "list[str]") -> None:
    """Write one grabber input file, in the shape iptv-org's own files use."""
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n'
        + "\n".join(entries)
        + "\n</channels>\n",
        encoding="utf-8",
    )


def container_path(host_path: Path, epg_dir: Path) -> str:
    """The path the container sees for a file under the EPG directory."""
    rel = host_path.resolve().relative_to(epg_dir.resolve())
    return f"{CONTAINER_PUBLIC}/{rel.as_posix()}"


def grabber_command(container: str, channels: str, output: str, days: int) -> "list[str]":
    """The one grab this script runs, spelled the way the image's own entrypoint does.

    `--days` is a flag rather than an environment variable here: the image's pm2
    config never passes it, which is why `DAYS` had to be set in the environment for
    the container's own (now disabled) grab to honour it.
    """
    return [
        "docker", "exec", container,
        "npm", "run", "grab", "--",
        f"--channels={channels}",
        f"--days={days}",
        f"--output={output}",
    ]


def is_oom(result) -> bool:
    """Whether a failed grab died of memory rather than of the site or the network.

    Only memory is worth retrying smaller: a site that answered 404 will answer 404
    for half its channels too, and splitting there just doubles the failures.
    """
    text = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return result.returncode in OOM_EXIT_CODES or any(m in text for m in OOM_MARKERS)


class SiteProgress:
    """What the run has proved about each site so far.

    `--max-channels` guesses that a grab's cost follows its channel count, and
    halving is what tests the guess. One site here fails the guess outright: its
    parts ran out of heap at 729 channels, at 365, at 182, and at 91 — and then a
    *single* channel of it exhausted a 3 GB heap in 214 seconds with
    `MAX_CONNECTIONS=1`. No split can fix that, and each attempt spends minutes of
    this host's memory (the same host runs Jellyfin) to learn nothing.

    So a site is asked once whether one channel fits. If it does not, the site is
    dropped for this run and reported once: a guide missing that site's listings is
    the same outcome as bisecting it to the floor, without the memory or the time.
    If it does, splitting continues as normal — a dense site whose halves do fit is
    exactly the case the splitting was built for. Next run probes again, so a site
    that recovers comes back with no change to anything.
    """

    def __init__(self) -> None:
        self.probed: "set[str]" = set()
        self.succeeded: "set[str]" = set()
        self.abandoned: "set[str]" = set()


def run_part(container: str, epg_dir: Path, parts_dir: Path, name: str,
             entries: "list[str]", days: int, outputs: "list[Path]",
             failed: "list[str]", *, dropped: "list[str]", site: "str | None" = None,
             progress: "SiteProgress | None" = None) -> None:
    """Grab one part, halving it if the grabber runs out of heap.

    The successful outputs are appended in the order the parts are run, which is what
    keeps the merge deterministic: a split part contributes its halves in place of
    itself, so the channel that `channels.xml` lists first still wins its slot.
    """
    site = site or name
    progress = progress if progress is not None else SiteProgress()
    # A site the run has given up on: its sibling parts are skipped rather than
    # retried, which is the difference between two wasted grabs and fifteen.
    if site in progress.abandoned:
        return

    def grab(part_name: str, part_entries: "list[str]"):
        inputs = parts_dir / f"channels-{part_name}.xml"
        output = parts_dir / f"guide-{part_name}.xml"
        write_channel_list(inputs, part_entries)
        # A part left by an earlier run must not be mistaken for this run's: the merge
        # takes whatever exists, so yesterday's `guide-<site>.xml` would be read as
        # today's listings for that site. Remove it first, and only trust what this
        # run writes.
        output.unlink(missing_ok=True)
        command = grabber_command(container, container_path(inputs, epg_dir),
                                  container_path(output, epg_dir), days)
        print(f"  {output.name}: " + " ".join(command[3:]), flush=True)
        return output, subprocess.run(command, capture_output=True, text=True)

    output, result = grab(name, entries)
    if result.returncode == 0 and output.exists():
        outputs.append(output)
        progress.succeeded.add(site)
        return

    if is_oom(result):
        if len(entries) > 1 and site not in progress.probed:
            # One channel is the cheapest honest answer to "can this site be grabbed
            # at all?", and the only one that separates a dense site from a broken
            # one before minutes are spent bisecting.
            progress.probed.add(site)
            probe_output, probe = grab(f"{site}-probe1", entries[:1])
            if probe.returncode == 0 and probe_output.exists():
                outputs.append(probe_output)
            else:
                progress.abandoned.add(site)
                print(f"  {site}: dropped for this run — a single channel of it does not "
                      f"fit the grabber's heap either, so {len(entries)} channel(s) were "
                      f"not retried smaller", file=sys.stderr)
                dropped.append(output.name)
                return

        if len(entries) > MIN_CHANNELS:
            pieces = chunk(entries, (len(entries) + 1) // 2)
            print(f"    out of heap at {len(entries)} channel(s) — retrying as "
                  f"{len(pieces)} part(s) of <= {len(pieces[0])}", file=sys.stderr)
            for index, piece in enumerate(pieces):
                run_part(container, epg_dir, parts_dir, f"{name}-split{index + 1}",
                         piece, days, outputs, failed, dropped=dropped, site=site,
                         progress=progress)
            return

    failed.append(output.name)
    tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
    print(f"    FAILED ({result.returncode}): " + " / ".join(tail), file=sys.stderr)


def merge_guides(parts: "list[Path]", out_path: Path) -> "dict[str, int]":
    """Write one XMLTV file from the part guides, without holding them all in memory.

    Channels come first, because that is the order XMLTV consumers expect, and they
    are deduplicated by id — the same channel legitimately appears in more than one
    part, since more than one site may publish it. Programmes are deduplicated by
    (channel, start) with the first part winning, and written as they are read: the
    parts together are hundreds of megabytes, and a tree of them would put the merge
    itself back in the position the sharding just got the grabber out of.
    """
    channels: dict[str, str] = {}
    order: list[str] = []
    for part in parts:
        for _, element in ET.iterparse(part, events=("end",)):
            if element.tag != "channel":
                continue
            channel_id = element.get("id")
            if channel_id and channel_id not in channels:
                channels[channel_id] = ET.tostring(element, encoding="unicode").strip()
                order.append(channel_id)
            element.clear()

    programmes = 0
    seen: set[tuple[str, str]] = set()
    with open(out_path, "w", encoding="utf-8") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write('<tv generator-info-name="monarch sharded epg">\n')
        for channel_id in order:
            out.write("  " + channels[channel_id] + "\n")

        for part in parts:
            for _, element in ET.iterparse(part, events=("end",)):
                if element.tag != "programme":
                    continue
                key = (element.get("channel") or "", element.get("start") or "")
                if not all(key) or key in seen:
                    element.clear()
                    continue
                seen.add(key)
                out.write(ET.tostring(element, encoding="unicode").strip() + "\n")
                programmes += 1
                element.clear()
        out.write("</tv>\n")

    return {"channels": len(order), "programmes": programmes}


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Build guide.xml from one grab per site.")
    parser.add_argument("--channels", default="/opt/epg/channels.xml",
                        help="the channel list /opt/epg/channels.xml, as written by monarch-init")
    parser.add_argument("--out", default="/opt/epg/guide.xml",
                        help="the merged guide Jellyfin reads")
    parser.add_argument("--parts-dir", default="/opt/epg/parts",
                        help="where the per-site inputs and part guides are written")
    parser.add_argument("--days", type=int, default=3,
                        help="days of listings each part asks for (default: 3)")
    parser.add_argument("--max-channels", type=int, default=900,
                        help="channels per grab, so a large site is split further (0 = one grab per site)")
    parser.add_argument("--container", default="iptv", help="the EPG container to run the grabs in")
    parser.add_argument("--dry-run", action="store_true",
                        help="write the channel files and print the commands without grabbing or merging")
    args = parser.parse_args(argv)

    epg_dir = Path(args.out).resolve().parent
    channels_path = Path(args.channels)
    parts_dir = Path(args.parts_dir)
    if not channels_path.exists():
        print(f"{channels_path} does not exist — monarch-init writes it "
              "(delete it and re-run the container's init to refresh)", file=sys.stderr)
        return 1

    parts_dir.mkdir(parents=True, exist_ok=True)
    grouped = parse_channels(channels_path.read_text(encoding="utf-8"))
    if not grouped:
        print(f"{channels_path} has no <channel> entries with a site attribute", file=sys.stderr)
        return 1

    # Each entry is (part name, its channels, the site they came from). The site is
    # carried separately so that a site split at `--max-channels` is still one site
    # to the abandonment rule below: dropping the first chunk must not cost a probe
    # of the second.
    plan: list[tuple[str, "list[str]", str]] = []
    for site, entries in grouped.items():
        pieces = chunk(entries, args.max_channels)
        for index, piece in enumerate(pieces):
            plan.append((site if len(pieces) == 1 else f"{site}-{index + 1}", piece, site))

    print(f"{sum(len(v) for v in grouped.values())} channel entries across {len(grouped)} site(s) "
          f"-> {len(plan)} grab(s) of <= {args.max_channels} channels, {args.days} day(s) each")

    if args.dry_run:
        for name, piece, _site in plan:
            inputs = parts_dir / f"channels-{name}.xml"
            write_channel_list(inputs, piece)
            print("  " + " ".join(grabber_command(
                args.container, container_path(inputs, epg_dir),
                container_path(parts_dir / f"guide-{name}.xml", epg_dir), args.days)))
        return 0

    failed: list[str] = []
    dropped: list[str] = []
    outputs: list[Path] = []
    progress = SiteProgress()
    for name, piece, site in plan:
        run_part(args.container, epg_dir, parts_dir, name, piece, args.days, outputs,
                 failed, dropped=dropped, site=site, progress=progress)

    # A part that still failed is left out rather than guessed at: the guide keeps
    # every other site's listings, and the summary says which ones are missing, which
    # is the difference between a short guide and a wrong one.
    if failed:
        print(f"  {len(failed)} part(s) failed and were left out: {', '.join(failed)}", file=sys.stderr)

    # A site the probe gave up on is the *policy*, not a failure (see SiteProgress),
    # and it used to be counted as one. That made this command exit 1 on every run of
    # a timer that is working exactly as designed — the unit went red daily, the
    # journal said the same thing every time, and the red stopped meaning anything.
    # So the two are counted apart: a dropped site is reported here and does not
    # change the exit code, a part that failed for any other reason still does.
    if dropped:
        print(f"  {len(dropped)} part(s) dropped for this run and left out "
              f"({', '.join(dropped)}) — its single channel does not fit the grabber's "
              f"heap, which the next run probes again", file=sys.stderr)

    merged = merge_guides(outputs, Path(args.out))
    print(f"wrote {args.out}: {merged['channels']} channel(s), {merged['programmes']} programme(s)")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
