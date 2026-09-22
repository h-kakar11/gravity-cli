"""Protocol-level tests for downloader.py's Phase 2 additions (inspect command, richer
error classification, filename/format-selector plumbing) -- unittest only, no pytest, no
extra deps (see test_downloader_selftest.py's docstring for why). Runs under the ambient
interpreter, which deliberately does NOT have yt_dlp installed -- every test here either
exercises a pure helper function directly or a code path that fails before ever touching
yt_dlp, so that absence is itself part of what a couple of these tests verify.
"""

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

DOWNLOADER_SCRIPT = Path(__file__).resolve().parents[1] / "vendor" / "downloader.py"


def _load_downloader_module():
    spec = importlib.util.spec_from_file_location("downloader", DOWNLOADER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


downloader = _load_downloader_module()


def run_command_stdin(payload: str):
    result = subprocess.run(
        [sys.executable, str(DOWNLOADER_SCRIPT), "--command-stdin"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    return result, events


class SanitizeFilenameTest(unittest.TestCase):
    def test_strips_illegal_windows_characters(self):
        self.assertEqual(downloader.sanitize_filename('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")

    def test_trims_trailing_dots_and_spaces(self):
        self.assertEqual(downloader.sanitize_filename("My Video... "), "My Video")

    def test_falls_back_to_download_when_nothing_survives(self):
        self.assertEqual(downloader.sanitize_filename(""), "download")
        self.assertEqual(downloader.sanitize_filename(None), "download")


class ClassifyExceptionTest(unittest.TestCase):
    def test_downloader_error_uses_its_own_classification_verbatim(self):
        exc = downloader.DownloaderError("E_PLAYLIST_NOT_SUPPORTED", "UNSUPPORTED_FORMAT", "nope")
        code, category, recoverable = downloader.classify_exception(exc)
        self.assertEqual(code, "E_PLAYLIST_NOT_SUPPORTED")
        self.assertEqual(category, "UNSUPPORTED_FORMAT")
        self.assertFalse(recoverable)

    def test_recognizes_private_video(self):
        code, category, _ = downloader.classify_exception(Exception("ERROR: Private video"))
        self.assertEqual(code, "E_VIDEO_PRIVATE")
        self.assertEqual(category, "PERMISSION_ERROR")

    def test_recognizes_removed_video(self):
        code, category, _ = downloader.classify_exception(
            Exception("This video has been removed by the uploader"))
        self.assertEqual(code, "E_VIDEO_REMOVED")
        self.assertEqual(category, "DOWNLOAD_FAILURE")

    def test_recognizes_geo_restriction(self):
        code, category, _ = downloader.classify_exception(
            Exception("The uploader has not made this video available in your country"))
        self.assertEqual(code, "E_GEO_RESTRICTED")
        self.assertEqual(category, "PERMISSION_ERROR")

    def test_recognizes_network_failure_by_type(self):
        # socket.timeout used to land here too; it now classifies as E_NETWORK_TIMEOUT --
        # see VersionCommandTest's neighbours below. A refused connection is the plain
        # network failure this test is about.
        code, category, recoverable = downloader.classify_exception(
            ConnectionRefusedError("Connection refused"))
        self.assertEqual(code, "E_NETWORK")
        self.assertEqual(category, "NETWORK_ERROR")
        self.assertTrue(recoverable)

    # --- Merge / post-processing failures -------------------------------------------
    # These fail after the bytes are already on disk, in yt-dlp's own ffmpeg step. They
    # used to fall through to E_DOWNLOAD_FAILED/UNKNOWN, which tells a retry policy
    # nothing -- the whole point of classifying them is the recoverable/permanent split.
    # The message strings below are yt-dlp's own (verified against its source at
    # 2026.8.19; the pattern table cites the exact files and lines).

    def test_missing_merge_tool_is_permanent(self):
        code, category, recoverable = downloader.classify_exception(Exception(
            "You have requested merging of multiple formats but ffmpeg is not installed. "
            "Aborting due to --abort-on-error"))
        self.assertEqual(code, "E_MERGE_TOOL_MISSING")
        self.assertEqual(category, "ENGINE_FAILURE")
        # Retrying cannot install ffmpeg; a retry would only re-download and re-fail.
        self.assertFalse(recoverable)

    def test_postprocessor_missing_ffmpeg_is_permanent(self):
        code, category, recoverable = downloader.classify_exception(Exception(
            "ffmpeg not found. Please install or provide the path using --ffmpeg-location"))
        self.assertEqual(code, "E_MERGE_TOOL_MISSING")
        self.assertEqual(category, "ENGINE_FAILURE")
        self.assertFalse(recoverable)

    def test_postprocessing_failure_is_permanent(self):
        code, category, recoverable = downloader.classify_exception(Exception(
            "Postprocessing: Error opening output files: Invalid argument"))
        self.assertEqual(code, "E_MERGE_FAILED")
        self.assertEqual(category, "ENGINE_FAILURE")
        self.assertFalse(recoverable)

    def test_fragment_transport_failure_is_recoverable(self):
        code, category, recoverable = downloader.classify_exception(Exception(
            "Unable to open fragment 17; HTTP Error 503: Service Unavailable"))
        self.assertEqual(code, "E_FRAGMENT_DOWNLOAD_FAILED")
        self.assertEqual(category, "NETWORK_ERROR")
        # The other half of the split: a fragment that failed to transfer genuinely often
        # succeeds on a second attempt.
        self.assertTrue(recoverable)

    def test_missing_fragment_is_permanent(self):
        code, _, recoverable = downloader.classify_exception(Exception(
            "fragment 4 not found, unable to continue"))
        self.assertEqual(code, "E_FRAGMENT_MISSING")
        self.assertFalse(recoverable)

    def test_a_more_specific_merge_pattern_wins_over_the_postprocessing_prefix(self):
        # yt-dlp prefixes the underlying message with "Postprocessing: ", so both patterns
        # match this string. Ordering in the table is what makes the specific one win --
        # without it every post-processing failure collapses to one opaque code.
        code, _, _ = downloader.classify_exception(Exception(
            "Postprocessing: ffmpeg not found. Please install or provide the path using "
            "--ffmpeg-location"))
        self.assertEqual(code, "E_MERGE_TOOL_MISSING")

    # --- Network timeouts ------------------------------------------------------------
    # Split out from E_NETWORK because a timeout is the network failure most likely to
    # succeed on a retry, and the retry policy reads the code as well as the flag.

    def test_socket_timeout_is_its_own_code(self):
        import socket
        code, category, recoverable = downloader.classify_exception(socket.timeout("timed out"))
        self.assertEqual(code, "E_NETWORK_TIMEOUT")
        self.assertEqual(category, "NETWORK_ERROR")
        self.assertTrue(recoverable)

    def test_a_host_that_does_not_resolve_is_not_a_timeout(self):
        import socket
        code, _, recoverable = downloader.classify_exception(
            socket.gaierror("Name or service not known"))
        self.assertEqual(code, "E_NETWORK")
        self.assertTrue(recoverable)

    def test_timeout_wording_wins_over_the_general_network_keywords(self):
        # "connection timed out" matches both tables; the timeout is the more specific
        # reading and must be checked first.
        code, _, _ = downloader.classify_exception(Exception("Connection timed out after 30s"))
        self.assertEqual(code, "E_NETWORK_TIMEOUT")

    def test_unrecognized_message_falls_back_to_unknown(self):
        code, category, _ = downloader.classify_exception(Exception("completely novel failure text"))
        self.assertEqual(code, "E_DOWNLOAD_FAILED")
        self.assertEqual(category, "UNKNOWN")


class VersionCommandTest(unittest.TestCase):
    """The version probe must answer even when there is nothing to probe -- "is the
    downloader usable" is exactly the question, so a missing yt_dlp is an answer, not an
    error.

    Upstream (Gravity) runs this suite under an interpreter that deliberately lacks
    yt_dlp. gravity-cli installs yt-dlp as a hard requirement, so here the absent case is
    guarded and the present case -- what this CLI actually runs against -- is asserted
    alongside it.
    """

    def test_reports_available_with_a_version_when_yt_dlp_is_installed(self):
        if downloader.yt_dlp is None:
            self.skipTest("this interpreter has no yt_dlp installed")
        result, events = run_command_stdin('{"command": "version", "params": {}}')
        self.assertEqual(result.returncode, 0)
        data = [e for e in events if e["event"] == "version"][0]["data"]
        self.assertTrue(data["available"])
        self.assertTrue(data["ytDlpVersion"])
        self.assertTrue(data["pythonVersion"])
        self.assertTrue(any(e["event"] == "completed" for e in events))

    def test_reports_unavailable_without_failing_when_yt_dlp_is_missing(self):
        if downloader.yt_dlp is not None:
            self.skipTest("this interpreter has yt_dlp installed")
        result, events = run_command_stdin('{"command": "version", "params": {}}')
        self.assertEqual(result.returncode, 0)
        version_events = [e for e in events if e["event"] == "version"]
        self.assertEqual(len(version_events), 1)
        data = version_events[0]["data"]
        self.assertFalse(data["available"])
        self.assertIsNone(data["ytDlpVersion"])
        self.assertFalse(data["stale"])
        # The interpreter itself is always identifiable, even when the library is not.
        self.assertTrue(data["pythonVersion"])
        self.assertTrue(any(e["event"] == "completed" for e in events))

    def test_a_dated_version_string_parses_into_a_release_date(self):
        import datetime
        self.assertEqual(downloader._yt_dlp_release_date("2026.8.19"),
                          datetime.date(2026, 8, 19))
        # yt-dlp nightlies append build information after the date.
        self.assertEqual(downloader._yt_dlp_release_date("2026.08.19.123456"),
                          datetime.date(2026, 8, 19))

    def test_an_undated_version_string_is_not_guessed_at(self):
        # A source checkout can report anything. Returning None means "age unknown", which
        # the C++ side renders as such rather than warning about a staleness it invented.
        for undated in ("", "unknown", "1.0", "v2026.8.19-git", "not.a.date.at.all"):
            self.assertIsNone(downloader._yt_dlp_release_date(undated), undated)


class FormatEntryTest(unittest.TestCase):
    def test_video_only_format(self):
        entry = downloader.format_entry({
            "format_id": "137", "ext": "mp4", "width": 1920, "height": 1080, "fps": 30,
            "vcodec": "avc1.640028", "acodec": "none", "vbr": 2500, "filesize": 123456,
        })
        self.assertTrue(entry["hasVideo"])
        self.assertFalse(entry["hasAudio"])
        self.assertEqual(entry["resolution"], "1920x1080")
        self.assertIsNone(entry["audioCodec"])

    def test_audio_only_format(self):
        entry = downloader.format_entry({
            "format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128,
        })
        self.assertFalse(entry["hasVideo"])
        self.assertTrue(entry["hasAudio"])
        self.assertIsNone(entry["resolution"])
        self.assertIsNone(entry["videoCodec"])


class BuildMetadataPayloadTest(unittest.TestCase):
    def test_single_video_payload(self):
        info = {
            "title": "Test Video", "uploader": "Someone", "duration": 42.0,
            "webpage_url": "https://example.com/watch?v=abc", "thumbnail": "https://example.com/t.jpg",
            "extractor_key": "Generic",
            "formats": [
                {"format_id": "137", "ext": "mp4", "vcodec": "avc1", "acodec": "none"},
                {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none"},  # storyboard, dropped
            ],
        }
        payload = downloader.build_metadata_payload(info, "https://example.com/watch?v=abc")
        self.assertEqual(payload["title"], "Test Video")
        self.assertEqual(payload["uploader"], "Someone")
        self.assertEqual(len(payload["formats"]), 1)  # storyboard-only format filtered out

    def test_playlist_raises_downloader_error(self):
        # Still an error on `inspect` even though playlists are supported now: this command
        # feeds DownloadJob, which downloads exactly one video. The frontend reads this code
        # as "re-ask via inspectPlaylist" -- see BuildPlaylistPayloadTest below.
        with self.assertRaises(downloader.DownloaderError) as ctx:
            downloader.build_metadata_payload({"_type": "playlist"}, "https://example.com/playlist?list=x")
        self.assertEqual(ctx.exception.code, "E_PLAYLIST_NOT_SUPPORTED")
        self.assertEqual(ctx.exception.category, "UNSUPPORTED_FORMAT")


class PlaylistUrlShapeTest(unittest.TestCase):
    """The `list=` ids that only look like playlists.

    A YouTube Mix ("radio") is an endless stream synthesized around a seed video, so
    enumerating one does not stop at "the songs in the playlist" -- it stops at
    _MAX_PLAYLIST_ENTRIES. That is how a 40-song playlist came to be reported as hundreds
    of videos: the link was copied while the mix was playing, not from the playlist page.
    The prefixes below were checked against live extractions (yt-dlp 2026.8.19); see the
    table in downloader.py.
    """

    def test_a_video_seeded_mix_is_reported_as_a_mix(self):
        self.assertEqual(
            downloader.auto_generated_mix_id(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ&start_radio=1"),
            "RDdQw4w9WgXcQ")

    def test_a_youtube_music_radio_is_reported_as_a_mix(self):
        self.assertEqual(
            downloader.auto_generated_mix_id(
                "https://music.youtube.com/watch?v=gJYjbDnyx-o&list=RDAMVMgJYjbDnyx-o"),
            "RDAMVMgJYjbDnyx-o")

    def test_an_ordinary_playlist_is_not_a_mix(self):
        self.assertIsNone(downloader.auto_generated_mix_id(
            "https://www.youtube.com/playlist?list=PLbpi6ZahtOH6Blw3RGYpWkSByi_T7Rygb"))

    def test_a_curated_music_playlist_is_not_a_mix(self):
        # RD-prefixed but a real, finite list (51 entries when checked) -- a bare
        # startswith("RD") test would wrongly refuse every YouTube Music curated playlist.
        self.assertIsNone(downloader.auto_generated_mix_id(
            "https://music.youtube.com/playlist?list=RDCLAK5uy_kLWIr9gv1XLlPbaDS965-Db4TrBoUTxQ8"))

    def test_a_non_youtube_list_id_starting_with_rd_is_not_a_mix(self):
        # "RD means radio" is a fact about YouTube's URL scheme and nothing else.
        self.assertIsNone(downloader.auto_generated_mix_id(
            "https://example.com/playlist?list=RDsomething"))

    def test_a_url_with_no_list_is_not_a_mix(self):
        self.assertIsNone(downloader.auto_generated_mix_id(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_a_playlist_seeded_mix_normalizes_back_to_its_playlist(self):
        # Pressing play on your own playlist in YouTube Music yields RDAMPL<playlist id>.
        # Left as-is yt-dlp resolves it to the seed video alone and the user is told their
        # playlist link is "a single video"; rewritten, it enumerates the real playlist.
        self.assertEqual(
            downloader.normalize_playlist_url(
                "https://music.youtube.com/watch?v=Vh4O04Bpovw&list=RDAMPLPL123&index=2"),
            "https://music.youtube.com/playlist?list=PL123")

    def test_a_normalized_playlist_seeded_mix_is_not_treated_as_a_mix(self):
        normalized = downloader.normalize_playlist_url(
            "https://music.youtube.com/watch?v=Vh4O04Bpovw&list=RDAMPLPL123")
        self.assertIsNone(downloader.auto_generated_mix_id(normalized))

    def test_normalize_leaves_every_other_url_alone(self):
        for url in ("https://www.youtube.com/playlist?list=PL123",
                    "https://www.youtube.com/watch?v=abc&list=RDabc",
                    "https://example.com/playlist?list=RDAMPLx",
                    "not a url at all"):
            self.assertEqual(downloader.normalize_playlist_url(url), url)


class InspectPlaylistMixRejectionTest(unittest.TestCase):
    """A mix is refused before the probe, so this holds with or without yt_dlp installed."""

    def test_mix_url_is_refused_with_its_own_code(self):
        result, events = run_command_stdin(json.dumps({
            "command": "inspectPlaylist",
            "params": {"url": "https://www.youtube.com/watch?v=abc&list=RDabc"}}))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(events[-1]["event"], "error")
        self.assertEqual(events[-1]["data"]["code"], "E_PLAYLIST_IS_MIX")
        self.assertEqual(events[-1]["data"]["category"], "UNSUPPORTED_FORMAT")


class BuildPlaylistPayloadTest(unittest.TestCase):
    @staticmethod
    def _playlist(entries):
        return {
            "_type": "playlist", "title": "My Playlist", "uploader": "Someone",
            "webpage_url": "https://example.com/playlist?list=x", "entries": entries,
        }

    def test_enumerates_entries_in_order_numbered_from_one(self):
        payload = downloader.build_playlist_payload(self._playlist([
            {"url": "https://example.com/watch?v=a", "title": "First", "duration": 10},
            {"url": "https://example.com/watch?v=b", "title": "Second", "duration": 20},
        ]), "https://example.com/playlist?list=x")

        self.assertEqual(payload["title"], "My Playlist")
        self.assertEqual(payload["count"], 2)
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["unavailableCount"], 0)
        self.assertEqual([e["index"] for e in payload["entries"]], [1, 2])
        self.assertEqual([e["title"] for e in payload["entries"]], ["First", "Second"])
        self.assertEqual(payload["entries"][0]["url"], "https://example.com/watch?v=a")

    def test_unavailable_entries_are_dropped_and_numbering_stays_contiguous(self):
        # yt-dlp yields None for a deleted/private entry. Passing those through would create
        # one job per dead entry, each failing on its own; and numbering must not leave a
        # hole where the dead entry was.
        payload = downloader.build_playlist_payload(self._playlist([
            {"url": "https://example.com/watch?v=a", "title": "First"},
            None,
            {"title": "No URL at all"},
            {"url": "https://example.com/watch?v=d", "title": "Fourth"},
        ]), "https://example.com/playlist?list=x")

        self.assertEqual(payload["count"], 2)
        self.assertEqual([e["index"] for e in payload["entries"]], [1, 2])
        self.assertEqual([e["title"] for e in payload["entries"]], ["First", "Fourth"])
        # Two entries dropped (None, and one with no resolvable URL) -- reported distinctly
        # from truncation, which this playlist never hit.
        self.assertEqual(payload["unavailableCount"], 2)
        self.assertFalse(payload["truncated"])

    def test_falls_back_to_webpage_url_when_entry_has_no_flat_url(self):
        payload = downloader.build_playlist_payload(self._playlist([
            {"webpage_url": "https://example.com/watch?v=z", "title": "Only webpage_url"},
        ]), "https://example.com/playlist?list=x")
        self.assertEqual(payload["entries"][0]["url"], "https://example.com/watch?v=z")

    def test_caps_fan_out_and_reports_truncation(self):
        oversized = [
            {"url": f"https://example.com/watch?v={n}", "title": f"Video {n}"}
            for n in range(downloader._MAX_PLAYLIST_ENTRIES + 25)
        ]
        payload = downloader.build_playlist_payload(
            self._playlist(oversized), "https://example.com/playlist?list=x")

        self.assertEqual(payload["count"], downloader._MAX_PLAYLIST_ENTRIES)
        self.assertTrue(payload["truncated"])

    def test_a_playlist_of_exactly_the_cap_is_not_reported_as_truncated(self):
        # `truncated` used to be `len(entries) >= cap`, which is also true when the playlist
        # happens to hold exactly `cap` complete videos and nothing was dropped -- the UI
        # then told the user entries had been left out when none had.
        exact = [
            {"url": f"https://example.com/watch?v={n}", "title": f"Video {n}"}
            for n in range(downloader._MAX_PLAYLIST_ENTRIES)
        ]
        payload = downloader.build_playlist_payload(
            self._playlist(exact), "https://example.com/playlist?list=x")

        self.assertEqual(payload["count"], downloader._MAX_PLAYLIST_ENTRIES)
        self.assertFalse(payload["truncated"])

    def test_unavailable_entries_past_the_cap_do_not_count_as_truncation(self):
        # Dead entries are dropped whether or not there is room for them, so a tail made
        # entirely of them is not a truncated playlist.
        entries = [
            {"url": f"https://example.com/watch?v={n}", "title": f"Video {n}"}
            for n in range(downloader._MAX_PLAYLIST_ENTRIES)
        ] + [None, {"title": "no url"}]
        payload = downloader.build_playlist_payload(
            self._playlist(entries), "https://example.com/playlist?list=x")

        self.assertEqual(payload["count"], downloader._MAX_PLAYLIST_ENTRIES)
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["unavailableCount"], 2)

    def test_entry_duration_is_emitted_under_the_key_the_core_parses(self):
        # The C++ side (YtDlpProvider::ParsePlaylistInfo) reads "duration" here, and read
        # "durationSeconds" for a while -- silently dropping every entry duration. Pinning
        # the producer's key so that mismatch cannot come back unnoticed.
        payload = downloader.build_playlist_payload(self._playlist([
            {"url": "https://example.com/watch?v=a", "title": "First", "duration": 61.0},
        ]), "https://example.com/playlist?list=x")

        self.assertIn("duration", payload["entries"][0])
        self.assertEqual(payload["entries"][0]["duration"], 61.0)
        self.assertNotIn("durationSeconds", payload["entries"][0])

    def test_single_video_raises_not_a_playlist(self):
        with self.assertRaises(downloader.DownloaderError) as ctx:
            downloader.build_playlist_payload(
                {"_type": "video", "title": "Just a video"}, "https://example.com/watch?v=a")
        self.assertEqual(ctx.exception.code, "E_NOT_A_PLAYLIST")
        self.assertEqual(ctx.exception.category, "UNSUPPORTED_FORMAT")

    def test_empty_playlist_yields_zero_entries_rather_than_failing(self):
        payload = downloader.build_playlist_payload(
            self._playlist([]), "https://example.com/playlist?list=x")
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["entries"], [])


class EscapeOuttmplLiteralTest(unittest.TestCase):
    """The output template's literal halves are attacker-influenced (a video title becomes
    the filename base), so they must not be able to introduce template fields."""

    def test_a_plain_name_is_unchanged(self):
        self.assertEqual(downloader.escape_outtmpl_literal("My Video"), "My Video")

    def test_a_percent_is_doubled_so_it_means_itself(self):
        self.assertEqual(downloader.escape_outtmpl_literal("Save 50% Now"), "Save 50%% Now")

    def test_an_injected_field_reference_is_neutralised(self):
        # Unescaped, yt-dlp expanded this into the real extension ("100mp4 weird").
        self.assertEqual(
            downloader.escape_outtmpl_literal("100%(ext)s weird"), "100%%(ext)s weird")

    def test_a_padding_attack_is_neutralised(self):
        # Unescaped, "%(title)200000s" rendered a 200,000-character filename.
        self.assertEqual(
            downloader.escape_outtmpl_literal("%(title)200000s"), "%%(title)200000s")


class YtDlpVersionLookupTest(unittest.TestCase):
    def test_returns_empty_string_when_yt_dlp_is_absent(self):
        if downloader.yt_dlp is not None:
            self.skipTest("ambient interpreter unexpectedly has yt_dlp installed")
        self.assertEqual(downloader._yt_dlp_version_string(), "")

    def test_reads_the_submodule_attribute_current_releases_actually_expose(self):
        # Current yt-dlp has no `yt_dlp.__version__` at all -- only
        # `yt_dlp.version.__version__` (verified against 2026.08.19). Reading the package
        # attribute reported no version for every real install, which silently disabled the
        # staleness warning. A stub module proves which name is consulted without needing
        # yt_dlp installed (this suite deliberately runs without it).
        import types

        fake_pkg = types.ModuleType("yt_dlp")
        fake_version_mod = types.ModuleType("yt_dlp.version")
        fake_version_mod.__version__ = "2026.08.19"
        fake_pkg.version = fake_version_mod  # type: ignore[attr-defined]
        # Deliberately NOT setting fake_pkg.__version__: that attribute does not exist on a
        # real modern build either.

        original_pkg = sys.modules.get("yt_dlp")
        original_version_mod = sys.modules.get("yt_dlp.version")
        original_ref = downloader.yt_dlp
        sys.modules["yt_dlp"] = fake_pkg
        sys.modules["yt_dlp.version"] = fake_version_mod
        downloader.yt_dlp = fake_pkg
        try:
            self.assertEqual(downloader._yt_dlp_version_string(), "2026.08.19")
        finally:
            downloader.yt_dlp = original_ref
            for name, value in (("yt_dlp", original_pkg), ("yt_dlp.version", original_version_mod)):
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value


class CommandStdinProtocolTest(unittest.TestCase):
    def test_malformed_json_emits_error_and_exits_nonzero(self):
        result, events = run_command_stdin("not json at all")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "error")

    def test_unsupported_command_emits_error(self):
        result, events = run_command_stdin(json.dumps({"command": "conjure", "params": {}}))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(events[0]["event"], "error")
        self.assertIn("unsupported command", events[0]["data"]["message"].lower())

    def test_inspect_missing_url_emits_error(self):
        result, events = run_command_stdin(json.dumps({"command": "inspect", "params": {}}))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(events[0]["event"], "error")

    def test_download_missing_required_keys_emits_error(self):
        result, events = run_command_stdin(json.dumps({"command": "download", "params": {"url": "x"}}))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(events[0]["event"], "error")

    def test_inspect_without_yt_dlp_installed_emits_clean_error(self):
        # This suite's ambient interpreter deliberately has no yt_dlp (see module
        # docstring) -- exercising exactly the "yt_dlp is not installed" guard path.
        if downloader.yt_dlp is not None:
            self.skipTest("ambient interpreter unexpectedly has yt_dlp installed")
        result, events = run_command_stdin(
            json.dumps({"command": "inspect", "params": {"url": "https://example.com/watch?v=x"}}))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(events[-1]["event"], "error")
        self.assertIn("yt_dlp", events[-1]["data"]["message"])

    def test_inspect_playlist_missing_url_emits_error(self):
        result, events = run_command_stdin(json.dumps({"command": "inspectPlaylist", "params": {}}))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(events[0]["event"], "error")

    def test_inspect_playlist_without_yt_dlp_installed_emits_clean_error(self):
        if downloader.yt_dlp is not None:
            self.skipTest("ambient interpreter unexpectedly has yt_dlp installed")
        result, events = run_command_stdin(json.dumps(
            {"command": "inspectPlaylist", "params": {"url": "https://example.com/playlist?list=x"}}))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(events[-1]["event"], "error")
        self.assertIn("yt_dlp", events[-1]["data"]["message"])


if __name__ == "__main__":
    unittest.main()
