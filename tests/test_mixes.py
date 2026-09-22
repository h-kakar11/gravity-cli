"""Covers URL classification, which decides which download flow a pasted link gets.

The underlying mix/playlist detection is Gravity's own (already covered by
test_downloader_protocol.py); what is tested here is gravity-cli's routing on top of it,
including the combo links where the user has to be asked which they meant.
"""

import unittest

from gravity_cli import mixes


class ClassifyTest(unittest.TestCase):
    def test_a_plain_video_is_a_video(self):
        kind = mixes.classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(kind.kind, mixes.VIDEO)
        self.assertIsNotNone(kind.video_url)

    def test_a_short_link_is_a_video(self):
        kind = mixes.classify("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(kind.kind, mixes.VIDEO)
        self.assertIsNotNone(kind.video_url)

    def test_a_bare_playlist_is_a_playlist_with_no_single_video_option(self):
        kind = mixes.classify("https://www.youtube.com/playlist?list=PL123")
        self.assertEqual(kind.kind, mixes.PLAYLIST)
        # Nothing to offer as "just this video" -- the link names no video.
        self.assertIsNone(kind.video_url)

    def test_a_combo_link_is_a_playlist_that_can_still_be_downloaded_as_one_video(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123"
        kind = mixes.classify(url)
        self.assertEqual(kind.kind, mixes.PLAYLIST)
        # The original URL is kept: the vendored script uses noplaylist=True, which
        # resolves a combo link to just the video.
        self.assertEqual(kind.video_url, url)

    def test_a_video_seeded_mix_is_a_mix(self):
        kind = mixes.classify(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ&start_radio=1")
        self.assertEqual(kind.kind, mixes.MIX)
        self.assertEqual(kind.mix_id, "RDdQw4w9WgXcQ")
        self.assertIsNotNone(kind.video_url)

    def test_a_youtube_music_radio_is_a_mix(self):
        kind = mixes.classify(
            "https://music.youtube.com/watch?v=gJYjbDnyx-o&list=RDAMVMgJYjbDnyx-o")
        self.assertEqual(kind.kind, mixes.MIX)

    def test_a_curated_music_playlist_is_not_a_mix(self):
        kind = mixes.classify(
            "https://music.youtube.com/playlist?list=RDCLAK5uy_kLWIr9gv1XLlPbaDS965-Db4TrBoUTxQ8")
        self.assertEqual(kind.kind, mixes.PLAYLIST)

    def test_a_playlist_seeded_mix_is_normalized_into_its_real_playlist(self):
        kind = mixes.classify(
            "https://music.youtube.com/watch?v=Vh4O04Bpovw&list=RDAMPLPL123&index=2")
        self.assertEqual(kind.kind, mixes.PLAYLIST)
        self.assertEqual(kind.url, "https://music.youtube.com/playlist?list=PL123")

    def test_a_non_youtube_list_id_starting_with_rd_is_not_a_mix(self):
        kind = mixes.classify("https://example.com/playlist?list=RDsomething")
        self.assertEqual(kind.kind, mixes.VIDEO)

    def test_surrounding_whitespace_is_ignored(self):
        kind = mixes.classify("  https://www.youtube.com/playlist?list=PL123  ")
        self.assertEqual(kind.kind, mixes.PLAYLIST)


class MixDisclaimerTest(unittest.TestCase):
    def test_the_disclaimer_states_the_cap_and_that_it_is_not_a_playlist(self):
        text = mixes.MIX_DISCLAIMER.format(limit=50)
        self.assertIn("50", text)
        self.assertIn("not a real playlist", text)
        self.assertNotIn("{limit}", text)


if __name__ == "__main__":
    unittest.main()
