#!/usr/bin/env python3
"""Unit tests for scripts/clipbucket-library.py.

What is pinned here is everything the app's own code decides and a mistake in it
would hide until someone looked at the site: the thumbnail file names
(`VideoThumbs::getThumbName`), when each thumbnail is taken, the filter
`FFMpeg::extractVideoThumbnail` uses, the length limits (`max_video_title`,
`file_name varchar(32)`) and the parsing that turns a folder of files into
titles — including the show whose own name contains the separator the file name
uses.

Nothing here needs docker or ffmpeg: the rendering is pure, and `scan_library`
is exercised against a temporary tree.

Run:  python3 -m unittest discover -s scripts/tests -v
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

spec = importlib.util.spec_from_file_location(
    "clipbucket_library", REPO / "scripts" / "clipbucket-library.py"
)
cl = importlib.util.module_from_spec(spec)
# dataclasses resolves the defining module by name, so it has to be registered
# before the module executes, not just returned by the loader.
sys.modules["clipbucket_library"] = cl
spec.loader.exec_module(cl)


class EpisodeParsing(unittest.TestCase):
    def test_the_plain_case(self):
        self.assertEqual(
            cl.parse_episode_name("WWE SmackDown - S28E35 - SmackDown 1410 HDTV-1080p"),
            ("WWE SmackDown", 28, 35, "SmackDown 1410"),
        )

    def test_a_show_whose_name_contains_the_separator(self):
        # The case that breaks a positional read: the token is third, not second.
        self.assertEqual(
            cl.parse_episode_name(
                "Star Trek - Strange New Worlds - S04E02 - The Griffin Incident WEBDL-1080p"
            ),
            ("Star Trek - Strange New Worlds", 4, 2, "The Griffin Incident"),
        )

    def test_a_lowercase_token_is_read(self):
        self.assertEqual(
            cl.parse_episode_name("lanterns - s01e02 - trust fall"),
            ("lanterns", 1, 2, "trust fall"),
        )

    def test_no_token_is_not_guessed_at(self):
        self.assertIsNone(cl.parse_episode_name("Some Movie 2019 1080p"))

    def test_a_bare_token_parses_with_no_show_of_its_own(self):
        # The show is the folder's job in scan_library, so a name carrying only
        # an episode token is still usable rather than refused.
        self.assertEqual(cl.parse_episode_name("S01E02"), ("", 1, 2, ""))

    def test_a_name_with_no_episode_title_is_allowed(self):
        self.assertEqual(cl.parse_episode_name("Lanterns - S01E03"), ("Lanterns", 1, 3, ""))


class QualityStripping(unittest.TestCase):
    def test_release_tags_go(self):
        self.assertEqual(cl.strip_quality("Pilot WEBDL-1080p"), "Pilot")
        self.assertEqual(cl.strip_quality("Trust Fall HDTV-720p"), "Trust Fall")
        self.assertEqual(cl.strip_quality("The Griffin Incident 1080p WEB-DL"), "The Griffin Incident")

    def test_a_title_that_is_only_a_tag_does_not_take_the_space_with_it(self):
        self.assertEqual(cl.strip_quality("Pilot"), "Pilot")

    def test_a_year_in_a_title_survives(self):
        self.assertEqual(cl.strip_quality("The Worst of The Late Show"), "The Worst of The Late Show")


class Titles(unittest.TestCase):
    def test_a_fitting_episode_title_is_used_whole(self):
        self.assertEqual(
            cl.render_title("episode", "Lanterns", 1, 1, "Pilot"),
            "Lanterns - S01E01 - Pilot",
        )

    def test_an_overlong_episode_falls_back_to_show_and_number(self):
        title = cl.render_title(
            "episode", "The Late Show with Stephen Colbert", 11, 115,
            "The Worst of The Late Show with Stephen Colbert",
        )
        self.assertEqual(title, "The Late Show with Stephen Colbert - S11E115")
        self.assertLessEqual(len(title), cl.MAX_TITLE)

    def test_every_rendered_title_is_inside_the_apps_limit(self):
        long_show = "A" * 200
        for title in (
            cl.render_title("movie", long_show, 0, 0, ""),
            cl.render_title("episode", long_show, 1, 1, "B" * 200),
        ):
            self.assertLessEqual(len(title), cl.MAX_TITLE)

    def test_a_cut_title_does_not_end_on_a_separator(self):
        self.assertFalse(cl.fit_title("star trek - strange new worlds - a" * 10).endswith("-"))


class FileNames(unittest.TestCase):
    def test_the_name_fits_the_column(self):
        item = cl.Item(
            kind="episode", source="/x", rel="tv/Star Trek - Strange New Worlds/x.mkv",
            category="TV Shows", title="t", tag="Star Trek - Strange New Worlds",
            season=4, episode=2,
        )
        self.assertLessEqual(len(item.file_name), cl.MAX_FILE_NAME)
        self.assertTrue(item.file_name.startswith("star-trek-stran-s04e02-"))

    def test_an_episode_number_survives_a_long_show_name(self):
        # Trimming the whole string instead would give S11E11 for S11E115, so
        # two episodes would be distinguishable only by their digest.
        item = cl.Item(
            kind="episode", source="/x", rel="tv/The Late Show/x.mkv",
            category="TV Shows", title="t", tag="The Late Show with Stephen Colbert",
            season=11, episode=115,
        )
        self.assertIn("s11e115", item.file_name)
        self.assertLessEqual(len(item.file_name), cl.MAX_FILE_NAME)
        self.assertNotIn("--", item.file_name)

    def test_the_name_is_derived_from_the_path_not_the_title(self):
        # The digest is over the source path, so a title edit cannot orphan a file.
        self.assertEqual(
            cl.file_name_for("tv/x.mkv", "lanterns-s01e01"),
            cl.file_name_for("tv/x.mkv", "lanterns-s01e01"),
        )
        self.assertNotEqual(
            cl.file_name_for("tv/a.mkv", "lanterns-s01e01"),
            cl.file_name_for("tv/b.mkv", "lanterns-s01e01"),
        )


class QualityLabels(unittest.TestCase):
    def test_a_letterboxed_1080p_film_is_labeled_1080(self):
        # 1920x804 is 1080p class; labeling it 804 would write a quality the
        # app's own converter never produces and the player never asks for.
        self.assertEqual(cl.quality_for(1920, 804), 1080)
        self.assertEqual(cl.quality_for(1920, 1040), 1080)

    def test_a_true_720p_frame_is_labeled_720(self):
        self.assertEqual(cl.quality_for(1280, 720), 720)
        self.assertEqual(cl.quality_for(640, 480), 480)

    def test_a_tiny_frame_still_gets_a_label(self):
        self.assertEqual(cl.quality_for(160, 120), 240)

    def test_the_file_name_is_the_one_get_video_files_builds(self):
        self.assertEqual(
            cl.media_name("coyote-vs-acme-2026-819edb3f", 1080),
            "coyote-vs-acme-2026-819edb3f-1080.mp4",
        )

    def test_only_a_named_for_the_app_file_counts_as_present(self):
        # A bare <file_name>.mp4 is read by update_video_files as a *resolution*:
        # it derives "819edb3f" and the player requests a file never written.
        self.assertFalse(cl.named_for_the_app(["coyote-vs-acme-2026-819edb3f.mp4"]))
        self.assertTrue(cl.named_for_the_app(["coyote-vs-acme-2026-819edb3f-1080.mp4"]))
        self.assertFalse(cl.named_for_the_app([]))

    def test_an_absent_or_empty_file_is_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope-1080.mp4")
            empty = os.path.join(tmp, "empty-1080.mp4")
            open(empty, "w").close()
            self.assertFalse(cl.media_is_complete(missing, 100))
            self.assertFalse(cl.media_is_complete(empty, 100))

    def test_a_file_ffprobe_cannot_read_is_not_complete(self):
        # What an interrupted remux actually leaves behind: bytes, not a video.
        with tempfile.TemporaryDirectory() as tmp:
            partial = os.path.join(tmp, "partial-1080.mp4")
            with open(partial, "wb") as fh:
                fh.write(b"\x00" * 4096)
            self.assertFalse(cl.media_is_complete(partial, 100))

    def test_a_stale_file_is_the_one_whose_name_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("x-1080.mp4", "x.mp4", "other-1080.mp4", "x-1080.srt"):
                open(os.path.join(tmp, name), "w").close()
            self.assertEqual(cl.stale_media(tmp, "x", "x-1080.mp4"), ["x.mp4"])


class MediaDecision(unittest.TestCase):
    def test_an_h264_aac_mp4_is_linked_not_copied(self):
        self.assertEqual(cl.media_decision("h264", "aac", "mp4"), "link")

    def test_how_ffprobe_actually_names_an_mp4_is_linked(self):
        # The regression that made the hardlink branch dead code: ffprobe
        # reports `.mp4` as this whole list, never as "mp4".
        self.assertEqual(cl.media_decision("h264", "aac", "mov,mp4,m4a,3gp,3g2,mj2"), "link")

    def test_matroska_is_remuxed(self):
        self.assertEqual(cl.media_decision("h264", "aac", "matroska"), "remux")

    def test_non_web_audio_is_remuxed(self):
        self.assertEqual(cl.media_decision("h264", "eac3", "mp4"), "remux")

    def test_an_mkv_is_not_mistaken_for_an_mp4(self):
        self.assertEqual(cl.media_decision("h264", "aac", "matroska,webm"), "remux")

    def test_hevc_is_remuxed_unless_the_transcode_is_asked_for(self):
        self.assertEqual(cl.media_decision("hevc", "eac3", "matroska"), "remux")
        self.assertEqual(cl.media_decision("hevc", "eac3", "matroska", True), "transcode")

    def test_an_exotic_video_codec_is_transcoded(self):
        self.assertEqual(cl.media_decision("mpeg4", "mp3", "avi"), "transcode")


class MediaCommand(unittest.TestCase):
    def test_a_remux_copies_the_video_and_faststarts(self):
        argv = cl.media_argv("/in/x.mkv", "/out/x.mp4", "remux", "h264")
        self.assertIn("-c:v", argv)
        self.assertEqual(argv[argv.index("-c:v") + 1], "copy")
        self.assertIn("+faststart", argv)
        self.assertNotIn("libx264", argv)

    def test_an_hevc_remux_gets_the_apple_tag(self):
        self.assertIn("hvc1", cl.media_argv("/in/x.mkv", "/out/x.mp4", "remux", "hevc"))

    def test_a_transcode_re_encodes_video_to_h264(self):
        argv = cl.media_argv("/in/x.mkv", "/out/x.mp4", "transcode", "hevc")
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
        self.assertIn("yuv420p", argv)

    def test_the_source_is_only_ever_read(self):
        for decision in ("remux", "transcode"):
            argv = cl.media_argv("/in/x.mkv", "/out/x.mp4", decision, "h264")
            self.assertEqual(argv[argv.index("-i") + 1], "/in/x.mkv")
            self.assertEqual(argv[-1], "/out/x.mp4")


class Thumbnails(unittest.TestCase):
    def test_the_file_name_matches_the_apps_own_rendering(self):
        # VideoThumbs::getThumbName() for a version greater than 5.5.2.
        self.assertEqual(
            cl.thumb_file_name("lanterns-s01e01-abc12345", "00001", "168x105"),
            "lanterns-s01e01-abc12345-thumbnail-00001-168x105.webp",
        )

    def test_thumb_numbers_are_padded_to_five(self):
        self.assertEqual(cl.thumb_num(1), "00001")
        self.assertEqual(cl.thumb_num(12), "00012")

    def test_thumbnails_are_taken_at_the_fractions_the_app_uses(self):
        # (int)(6167 / 5) * i, which is what ClipBucket computes.
        self.assertEqual(cl.thumb_times(6167), [1233, 2466, 3699, 4932, 6165])

    def test_a_short_video_gets_fewer_thumbnails(self):
        # min(num_thumbs, duration) frames, one per second once the video is
        # shorter than the configured count — the app's own arithmetic.
        self.assertEqual(cl.thumb_times(3), [1, 2, 3])

    def test_the_resolution_set_is_the_apps_own(self):
        self.assertEqual(
            [tag for tag, _w, _h in cl.THUMB_RESOLUTIONS],
            ["original", "168x105", "416x260", "512x320", "768x480"],
        )

    def test_the_command_is_the_apps_own(self):
        argv = cl.thumb_argv("/in/x.mp4", "/out/t.webp", 600, "168x105", 168, 105)
        self.assertIn("yuvj422p", argv)
        self.assertIn("libwebp", argv)
        # -ss before -i: seek, do not decode from the start.
        self.assertLess(argv.index("-ss"), argv.index("-i"))
        self.assertEqual(argv[argv.index("-ss") + 1], "600")
        scale = argv[argv.index("-vf") + 1]
        self.assertIn("scale=", scale)
        self.assertIn("pad=168:105", scale)

    def test_an_original_size_thumbnail_is_not_scaled(self):
        argv = cl.thumb_argv("/in/x.mp4", "/out/t.webp", 600, "original", None, None)
        self.assertNotIn("-vf", argv)


class LibraryScan(unittest.TestCase):
    def _tree(self, tmp: str) -> None:
        movies = os.path.join(tmp, "movies", "The Odyssey (2026)")
        os.makedirs(movies)
        for name, size in (
            ("The Odyssey (2026) WEBRip-1080p.mp4", 200),
            (".The Odyssey (2026) WEBRip-1080p.mp4.part", 5000),  # a partial download
            ("The Odyssey (2026) WEBRip-1080p.srt", 10),  # a sidecar
        ):
            with open(os.path.join(movies, name), "wb") as fh:
                fh.write(b"0" * size)

        tv = os.path.join(tmp, "tv", "Star Trek - Strange New Worlds")
        os.makedirs(tv)
        for name in (
            "Star Trek - Strange New Worlds - S04E02 - The Griffin Incident WEBDL-1080p.mkv",
            "Star Trek - Strange New Worlds - S04E02 - The Griffin Incident WEBDL-1080p.nfo",
            "Bonus Feature.mkv",  # no season/episode token
        ):
            with open(os.path.join(tv, name), "wb") as fh:
                fh.write(b"0")

    def test_it_finds_the_films_and_the_episodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            items, unparsed = cl.scan_library(tmp)
            kinds = sorted(i.kind for i in items)
            self.assertEqual(kinds, ["episode", "movie"])
            self.assertEqual(len(unparsed), 1)
            self.assertIn("Bonus Feature.mkv", unparsed[0])

    def test_the_folder_names_the_film_and_the_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            items, _ = cl.scan_library(tmp)
            movie = next(i for i in items if i.kind == "movie")
            episode = next(i for i in items if i.kind == "episode")
            self.assertEqual(movie.title, "The Odyssey (2026)")
            self.assertEqual(movie.category, "Movies")
            self.assertEqual(episode.tag, "Star Trek - Strange New Worlds")
            self.assertEqual(
                episode.title,
                "Star Trek - Strange New Worlds - S04E02 - The Griffin Incident",
            )

    def test_the_description_says_where_it_came_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            items, _ = cl.scan_library(tmp)
            episode = next(i for i in items if i.kind == "episode")
            self.assertIn("season 4, episode 2", episode.description)
            self.assertIn(episode.rel, episode.description)

    def test_a_missing_library_is_not_an_error_here(self):
        with tempfile.TemporaryDirectory() as tmp:
            items, unparsed = cl.scan_library(os.path.join(tmp, "nope"))
            self.assertEqual((items, unparsed), ([], []))


class Verdict(unittest.TestCase):
    def _facts(self, **over):
        facts = {
            "docker": True,
            "container_running": True,
            "ffmpeg": True,
            "ffprobe": True,
            "files_path": "/var/lib/docker/volumes/x/_data",
            "media_root": "/data/media",
            "media_root_is_dir": True,
            "install_version": cl.CORE_VERSION,
        }
        facts.update(over)
        return facts

    def test_a_deployment_with_everything_is_judgeable(self):
        cl.evaluate(self._facts())  # must not raise

    def test_no_docker_is_cannot_tell_not_a_finding(self):
        with self.assertRaises(cl.CantTell):
            cl.evaluate(self._facts(docker=False))

    def test_a_stopped_container_is_cannot_tell(self):
        with self.assertRaises(cl.CantTell):
            cl.evaluate(self._facts(container_running=False))

    def test_a_missing_media_root_is_cannot_tell(self):
        with self.assertRaises(cl.CantTell):
            cl.evaluate(self._facts(media_root_is_dir=False))

    def test_a_version_the_script_does_not_know_is_refused(self):
        with self.assertRaises(cl.CantTell):
            cl.evaluate(self._facts(install_version="5.4.0"))


class ReusableCopy(unittest.TestCase):
    """When an existing file may be left alone — the difference `--reencode-hevc` makes."""

    def test_a_complete_h264_copy_is_left_alone(self):
        with mock.patch.object(cl, "media_is_complete", return_value=True), \
             mock.patch.object(cl, "probe", return_value={"video_codec": "h264"}):
            self.assertTrue(cl.copy_is_reusable("/v/x-1080.mp4", 100, False))

    def test_an_hevc_copy_is_left_alone_when_h264_was_not_asked_for(self):
        with mock.patch.object(cl, "media_is_complete", return_value=True), \
             mock.patch.object(cl, "probe", return_value={"video_codec": cl.HEVC}):
            self.assertTrue(cl.copy_is_reusable("/v/x-1080.mp4", 100, False))

    def test_an_hevc_copy_is_rebuilt_when_h264_was_asked_for(self):
        # The whole reason the flag exists: without this the second run would
        # report the flagged items as "already there" and convert nothing.
        with mock.patch.object(cl, "media_is_complete", return_value=True), \
             mock.patch.object(cl, "probe", return_value={"video_codec": cl.HEVC}):
            self.assertFalse(cl.copy_is_reusable("/v/x-1080.mp4", 100, True))

    def test_a_converted_copy_is_left_alone_on_a_second_transcode_run(self):
        with mock.patch.object(cl, "media_is_complete", return_value=True), \
             mock.patch.object(cl, "probe", return_value={"video_codec": "h264"}):
            self.assertTrue(cl.copy_is_reusable("/v/x-1080.mp4", 100, True))

    def test_an_incomplete_file_is_never_reusable(self):
        with mock.patch.object(cl, "media_is_complete", return_value=False), \
             mock.patch.object(cl, "probe", return_value={"video_codec": "h264"}):
            self.assertFalse(cl.copy_is_reusable("/v/x-1080.mp4", 100, False))

    def test_a_file_ffprobe_cannot_read_is_not_called_good(self):
        with mock.patch.object(cl, "media_is_complete", return_value=True), \
             mock.patch.object(cl, "probe", side_effect=cl.ImportError_("no")):
            self.assertFalse(cl.copy_is_reusable("/v/x-1080.mp4", 100, True))

    def test_the_replacement_says_which_reason_it_was(self):
        # "replaced an incomplete file" over a file that was whole reads as data
        # loss, so the note a transcode run prints has to distinguish them.
        item = cl.Item(
            kind="episode", source="/lib/tv/Show/e.mkv", rel="tv/Show/e.mkv",
            category="TV Shows", title="Show - S01E01 - Pilot", tag="Show",
            season=1, episode=1,
        )
        source_facts = {"width": 1920, "height": 1080, "duration": 100,
                        "video_codec": cl.HEVC, "audio_codec": "aac", "container": "matroska"}
        with tempfile.TemporaryDirectory() as videos:
            dest = os.path.join(
                videos, cl.media_name(item.file_name, cl.quality_for(1920, 1080))
            )
            with open(dest, "wb") as fh:
                fh.write(b"0" * 16)
            with mock.patch.object(cl, "media_is_complete", return_value=True), \
                 mock.patch.object(cl, "probe", return_value={"video_codec": cl.HEVC, "duration": 100}), \
                 mock.patch.object(cl, "run") as runner:
                # The conversion's own artifact: the real ffmpeg writes it, so
                # the stand-in has to as well or the caller sees a failed run.
                def wrote_something(_argv, *_args, **_kwargs):
                    with open(dest, "wb") as fh:
                        fh.write(b"0" * 16)
                    return mock.Mock(returncode=0)

                runner.side_effect = wrote_something
                note, _quality = cl.materialise_media(item, source_facts, videos, True)
            self.assertNotIn("incomplete", note)
            self.assertIn("replaced an HEVC stream copy", note)

    def test_an_unchanged_item_is_left_alone(self):
        item = cl.Item(
            kind="episode", source="/lib/tv/Show/e.mkv", rel="tv/Show/e.mkv",
            category="TV Shows", title="Show - S01E01 - Pilot", tag="Show",
            season=1, episode=1,
        )
        source_facts = {"width": 1920, "height": 1080, "duration": 100,
                        "video_codec": cl.HEVC, "audio_codec": "aac", "container": "matroska"}
        with tempfile.TemporaryDirectory() as videos:
            with mock.patch.object(cl, "media_is_complete", return_value=True), \
                 mock.patch.object(cl, "probe", return_value={"video_codec": "h264"}), \
                 mock.patch.object(cl, "run") as runner:
                note, _quality = cl.materialise_media(item, source_facts, videos, True)
        self.assertEqual(note, "already there")
        runner.assert_not_called()


class SeriesGroups(unittest.TestCase):
    def _item(self, kind, tag, season=0, episode=0):
        return cl.Item(
            kind=kind, source=f"/lib/{tag}.mp4", rel=f"{tag}.mp4",
            category="TV Shows" if kind == "episode" else "Movies",
            title=tag, tag=tag, season=season, episode=episode,
        )

    def test_episodes_group_under_their_show_in_order(self):
        items = [
            self._item("episode", "Lanterns", 1, 3),
            self._item("episode", "Lanterns", 1, 1),
            self._item("episode", "Dune", 1, 1),
        ]
        groups = cl.series_of(items)
        self.assertEqual(sorted(groups), ["Dune", "Lanterns"])
        self.assertEqual([i.episode for i in groups["Lanterns"]], [1, 3])

    def test_a_film_is_not_a_series_of_one(self):
        self.assertEqual(cl.series_of([self._item("movie", "The Odyssey (2026)")]), {})


class FakeDb:
    """The container's mysql, small enough to reason about but stateful.

    `cb_collection_items` is what a re-run gets wrong, so the fake applies the
    statements it is given instead of only recording them: a second run that
    duplicated rows would show up as more rows.
    """

    def __init__(self, collections=None, items=None):
        self.collections = list(collections or [])
        self.items = list(items or [])
        self.statements = []
        self.next_id = 100

    def query(self, _container, sql):
        self.statements.append(sql)
        if "FROM cb_collections WHERE" in sql:
            named = [(cid, name) for cid, name in self.collections if f"'{name}'" in sql]
            if "SELECT collection_name" in sql:  # the check reads name -> id
                return [[name, str(cid)] for cid, name in named]
            return [[str(cid)] for cid, _name in named]  # the apply reads id only
        if "SELECT object_id FROM cb_collection_items" in sql:
            cid = int(sql.split("collection_id=")[1].split()[0])
            return [[str(oid)] for c, oid in self.items if c == cid and oid is not None]
        if "SELECT collection_id, object_id FROM cb_collection_items" in sql:
            return [[str(c), str(o)] for c, o in self.items]
        raise AssertionError(f"unexpected query: {sql}")

    def exec(self, _container, sql):
        self.statements.append(sql)
        if sql.startswith("INSERT INTO cb_collection_items"):
            for cid, oid in re.findall(r"\((\d+), (\d+), \d+, 'videos'", sql):
                self.items.append((int(cid), int(oid)))
        elif sql.startswith("DELETE FROM cb_collection_items"):
            cid = int(sql.split("collection_id=")[1].split()[0])
            doomed = {int(x) for x in re.findall(r"\d+", sql.split("IN (")[1])}
            self.items = [(c, o) for c, o in self.items if not (c == cid and o in doomed)]
        elif sql.startswith("INSERT INTO cb_collections"):
            name = re.search(r"VALUES \('([^']*)'", sql).group(1)
            self.collections.append((self.next_id, name))

    def insert_id(self, container, sql):
        self.exec(container, sql)
        self.next_id += 1
        return self.next_id - 1


class Collections(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        patched = [
            mock.patch.object(cl, "mysql_query", self.db.query),
            mock.patch.object(cl, "mysql_exec", self.db.exec),
            mock.patch.object(cl, "mysql_insert_id", self.db.insert_id),
        ]
        for p in patched:
            p.start()
            self.addCleanup(p.stop)

    def _sync(self, name, videoids):
        return cl.sync_collection("c", name, videoids, 7, "2026-01-01 00:00:00")

    def test_a_new_series_gets_a_collection_holding_its_episodes_in_order(self):
        cid, note = self._sync("Lanterns", [30, 10, 20])
        self.assertEqual(self.db.collections, [(cid, "Lanterns")])
        self.assertEqual([oid for _c, oid in self.db.items], [30, 10, 20])
        self.assertIn("+3", note)

    def test_the_collection_is_listed_under_the_episodes_category(self):
        cid, _ = self._sync("Lanterns", [10])
        self.assertTrue(any(
            s.startswith("INSERT IGNORE INTO cb_collections_categories") and f"({cid}, 7)" in s
            for s in self.db.statements
        ))

    def test_the_row_carries_every_column_the_table_requires(self):
        sql = cl.collection_insert_sql("Lanterns", "Every episode (2).", "2026-01-01 00:00:00", 10)
        for column in ("broadcast", "active", "public_upload", "type", "collection_name"):
            self.assertIn(column, sql)
        self.assertIn("'public'", sql)
        self.assertIn("'yes'", sql)

    def test_a_second_run_adds_nothing(self):
        cid, _ = self._sync("Lanterns", [10, 20])
        _cid, note = self._sync("Lanterns", [10, 20])
        self.assertEqual(len(self.db.items), 2)
        self.assertEqual([c for c, _o in self.db.items], [cid, cid])
        self.assertIn("in sync", note)

    def test_an_episode_that_left_the_library_leaves_the_collection(self):
        cid, _ = self._sync("Lanterns", [10, 20])
        self.db.items.append((cid, 99))  # e.g. an episode deleted from the library
        _cid, note = self._sync("Lanterns", [10, 20])
        self.assertEqual([oid for _c, oid in self.db.items], [10, 20])
        self.assertIn("-1", note)

    def test_a_new_episode_is_appended_without_touching_the_rest(self):
        cid, _ = self._sync("Lanterns", [10, 20])
        _cid, note = self._sync("Lanterns", [10, 20, 30])
        self.assertEqual([c for c, _o in self.db.items], [cid] * 3)
        self.assertIn("+1", note)

    def test_an_existing_collection_is_made_visible_again(self):
        self.db.collections.append((55, "Lanterns"))
        cid, _ = self._sync("Lanterns", [10])
        self.assertEqual(cid, 55)
        self.assertTrue(any(s.startswith("UPDATE cb_collections SET active='yes'") for s in self.db.statements))

    def test_a_show_with_no_episodes_is_not_a_collection(self):
        cid, note = self._sync("Lanterns", [])
        self.assertEqual((cid, note), (0, "no episodes"))
        self.assertEqual(self.db.collections, [])


class CollectionFindings(unittest.TestCase):
    """What `--check` says when the collection half is behind."""

    def setUp(self):
        self.db = FakeDb()
        patched = [
            mock.patch.object(cl, "mysql_query", self.db.query),
            mock.patch.object(cl, "mysql_exec", self.db.exec),
            mock.patch.object(cl, "mysql_insert_id", self.db.insert_id),
        ]
        for p in patched:
            p.start()
            self.addCleanup(p.stop)
        self.facts = {"container": "c"}

    def _series(self, episodes=2):
        items = [
            cl.Item(
                kind="episode", source=f"/lib/e{n}.mkv", rel=f"tv/L/e{n}.mkv",
                category="TV Shows", title=f"Lanterns - S01E0{n} - x", tag="Lanterns",
                season=1, episode=n,
            )
            for n in range(1, episodes + 1)
        ]
        return cl.series_of(items), items

    def test_no_collection_is_a_finding(self):
        series, items = self._series()
        ids = {i.file_name: n for n, i in enumerate(items, start=1)}
        self.assertEqual(cl.missing_collections(series, ids, self.facts), ["Lanterns"])

    def test_a_collection_holding_exactly_its_episodes_is_not(self):
        series, items = self._series()
        ids = {i.file_name: n for n, i in enumerate(items, start=1)}
        self.db.collections.append((3, "Lanterns"))
        self.db.items.extend((3, v) for v in ids.values())
        self.assertEqual(cl.missing_collections(series, ids, self.facts), [])

    def test_a_collection_missing_one_episode_is_a_finding(self):
        series, items = self._series()
        ids = {i.file_name: n for n, i in enumerate(items, start=1)}
        self.db.collections.append((3, "Lanterns"))
        self.db.items.append((3, ids[items[0].file_name]))
        self.assertEqual(cl.missing_collections(series, ids, self.facts), ["Lanterns"])

    def test_a_show_whose_episodes_have_no_rows_yet_is_not_reported_twice(self):
        series, _items = self._series()
        self.assertEqual(cl.missing_collections(series, {}, self.facts), [])


class Serving(unittest.TestCase):
    """`--serve-check`: the app's own answer, not this tool's model of it.

    The model — the file the app derives from a row — is the thing every other
    check here trusts. This one asks the watch page instead, because the way the
    model is wrong is invisible: the row is right, the file is there, and the
    page has no playable source on it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.files = os.path.join(self.tmp.name, "volume")
        os.makedirs(os.path.join(self.files, "upload", "files", "videos", "imported"))
        self.item = cl.Item(
            kind="movie",
            source="/data/media/movies/Film (2026)/Film (2026) WEBRip-1080p.mp4",
            rel="movies/Film (2026)/Film (2026) WEBRip-1080p.mp4",
            category="Movies",
            title="Film (2026)",
        )
        self.facts = {"container": "clipbucket", "files_path": self.files}

    @property
    def _source_url(self) -> str:
        return f"https://tube.example/files/videos/imported/{self.item.file_name}-1080.mp4"

    def _on_disk(self) -> str:
        path = cl.source_file(self._source_url, self.files)
        with open(path, "wb") as fh:
            fh.write(b"0" * 16)
        return path

    def _report(self, page, code=206):
        with mock.patch.object(cl, "watch_page", page), mock.patch.object(
            cl, "serves_bytes", lambda container, url: code
        ):
            return cl.serve_report([self.item], self.facts, {self.item.file_name: 7})

    def test_it_reads_the_sources_the_page_emits(self):
        body = (
            "<video><source src='https://tube.example/files/videos/imported/a-1080.mp4' "
            "type=\"video/mp4\"/>"
            "<source src=\"https://tube.example/files/videos/imported/a-720.mp4\"/></video>"
        )
        self.assertEqual(
            cl.page_sources(body),
            [
                "https://tube.example/files/videos/imported/a-1080.mp4",
                "https://tube.example/files/videos/imported/a-720.mp4",
            ],
        )

    def test_a_files_url_maps_onto_the_volume(self):
        # The web root is <volume>/upload, the same arithmetic the apply uses.
        self.assertEqual(
            cl.source_file("https://tube.example/files/videos/imported/a-1080.mp4", "/vol"),
            "/vol/upload/files/videos/imported/a-1080.mp4",
        )

    def test_a_url_outside_files_has_no_file_of_ours(self):
        self.assertEqual(cl.source_file("https://tube.example/player/video.js", "/vol"), "")

    def test_a_page_serving_a_real_file_is_no_problem(self):
        self._on_disk()
        problems = self._report(lambda c, v: (200, f"<source src='{self._source_url}'/>"))
        self.assertEqual(problems, [])

    def test_a_page_with_no_source_is_reported(self):
        # The first pass's rows: complete by every filesystem check, playing
        # nowhere, because the app built a different name than the tool wrote.
        self._on_disk()
        problems = self._report(lambda c, v: (200, "<video>nothing playable</video>"))
        self.assertIn("emits no <source>", problems[0][1])
        self.assertEqual(problems[0][0], self.item.file_name)

    def test_a_source_with_no_file_behind_it_is_reported(self):
        problems = self._report(
            lambda c, v: (
                200,
                "<source src='https://tube.example/files/videos/imported/gone-1080.mp4'/>",
            )
        )
        self.assertIn("no file at", problems[0][1])

    def test_a_file_the_web_server_will_not_serve_is_reported(self):
        # What `ls` cannot see: readable by root, 403 to nginx.
        self._on_disk()
        problems = self._report(
            lambda c, v: (200, f"<source src='{self._source_url}'/>"), code=403
        )
        self.assertIn("HTTP 403", problems[0][1])

    def test_a_watch_page_that_is_not_200_is_reported(self):
        problems = self._report(lambda c, v: (404, "nope"))
        self.assertIn("HTTP 404", problems[0][1])

    def test_an_item_with_no_row_is_reported(self):
        problems = cl.serve_report([self.item], self.facts, {})
        self.assertIn("no cb_video row", problems[0][1])

    def test_a_fetch_that_could_not_run_is_a_finding_not_a_traceback(self):
        def boom(container, videoid):
            raise cl.ImportError_("curl is not there")

        problems = self._report(boom)
        self.assertIn("curl is not there", problems[0][1])


class ServePreconditions(unittest.TestCase):
    """Serving an item needs no encoder, so a media host is never "cannot tell"."""

    def _facts(self, **over):
        facts = {
            "docker": True,
            "container_running": True,
            "files_path": "/vol",
            "media_root": "/data/media",
            "media_root_is_dir": True,
            "install_version": cl.CORE_VERSION,
            "ffmpeg": False,
            "ffprobe": False,
        }
        facts.update(over)
        return facts

    def test_it_judges_without_ffmpeg_on_the_host(self):
        cl.evaluate_serve(self._facts())

    def test_without_a_container_it_is_cannot_tell(self):
        with self.assertRaises(cl.CantTell):
            cl.evaluate_serve(self._facts(container_running=False))

    def test_a_version_this_script_does_not_know_is_cannot_tell(self):
        with self.assertRaises(cl.CantTell):
            cl.evaluate_serve(self._facts(install_version="5.4.0"))


class Sql(unittest.TestCase):
    def test_quotes_are_escaped(self):
        self.assertEqual(cl.sql_quote("It's a test"), "It''s a test")

    def test_backslashes_are_escaped(self):
        self.assertEqual(cl.sql_quote("a\\b"), "a\\\\b")


if __name__ == "__main__":
    unittest.main()
