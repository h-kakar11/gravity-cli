"""Covers the port of Gravity's FilenameSanitizer, plus the base-name assembly order."""

import os
import tempfile
import unittest

from gravity_cli import filenames, playlist


class SanitizeWindowsFilenameTest(unittest.TestCase):
    def test_replaces_every_illegal_windows_character(self):
        self.assertEqual(filenames.sanitize_windows_filename('a<b>c:d"e/f\\g|h?i*j'),
                          "a_b_c_d_e_f_g_h_i_j")

    def test_drops_control_characters_rather_than_replacing_them(self):
        self.assertEqual(filenames.sanitize_windows_filename("a\x01b\x1fc"), "abc")

    def test_trims_trailing_dots_and_spaces(self):
        self.assertEqual(filenames.sanitize_windows_filename("My Video... "), "My Video")

    def test_falls_back_to_untitled_when_nothing_legal_survives(self):
        self.assertEqual(filenames.sanitize_windows_filename(""), "untitled")
        self.assertEqual(filenames.sanitize_windows_filename("..."), "untitled")
        self.assertEqual(filenames.sanitize_windows_filename(None), "untitled")

    def test_leaves_unicode_and_emoji_untouched(self):
        self.assertEqual(filenames.sanitize_windows_filename("Sónar 2026 \U0001f3b5"),
                          "Sónar 2026 \U0001f3b5")

    def test_replaces_lone_surrogates_that_would_break_a_filesystem_call(self):
        # Unpaired surrogates survive a JSON round-trip but raise UnicodeEncodeError the
        # moment they reach a path.
        self.assertEqual(filenames.sanitize_windows_filename("bad\ud800title"),
                          "bad_title")

    def test_caps_length_at_two_hundred_characters(self):
        self.assertEqual(len(filenames.sanitize_windows_filename("x" * 500)), 200)

    def test_reserved_device_names_are_suffixed_with_and_without_an_extension(self):
        self.assertEqual(filenames.sanitize_windows_filename("NUL"), "NUL_file")
        self.assertEqual(filenames.sanitize_windows_filename("nul.txt"), "nul_file.txt")
        self.assertEqual(filenames.sanitize_windows_filename("COM1"), "COM1_file")

    def test_a_name_merely_containing_a_reserved_word_is_left_alone(self):
        self.assertEqual(filenames.sanitize_windows_filename("CONCERT"), "CONCERT")


class TruncateBaseNameForMaxPathTest(unittest.TestCase):
    def test_a_name_that_already_fits_is_unchanged(self):
        self.assertEqual(filenames.truncate_base_name_for_max_path("C:\\Videos", "Short"),
                          "Short")

    def test_a_long_name_is_trimmed_to_fit_the_legacy_max_path_budget(self):
        directory = "C:\\Videos"
        result = filenames.truncate_base_name_for_max_path(directory, "x" * 400)
        self.assertLess(len(result), 400)
        self.assertLessEqual(len(directory) + 1 + len(result) + 20, 259)

    def test_a_directory_with_no_remaining_budget_leaves_the_name_alone(self):
        self.assertEqual(
            filenames.truncate_base_name_for_max_path("C:\\" + "d" * 300, "name"), "name")

    def test_truncation_never_splits_a_multibyte_character(self):
        result = filenames.truncate_base_name_for_max_path("C:\\Videos", "é" * 300)
        self.assertNotIn("\ufffd", result)
        result.encode("utf-8")  # must still be encodable


class DeduplicateBaseNameTest(unittest.TestCase):
    def test_a_free_name_is_returned_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(filenames.deduplicate_base_name(directory, "Video"), "Video")

    def test_a_taken_name_gets_the_first_free_numbered_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            # Any extension counts as taken: the final container is not known until
            # yt-dlp has finished merging.
            open(os.path.join(directory, "Video.mp4"), "w").close()
            self.assertEqual(filenames.deduplicate_base_name(directory, "Video"),
                              "Video (1)")

            open(os.path.join(directory, "Video (1).webm"), "w").close()
            self.assertEqual(filenames.deduplicate_base_name(directory, "Video"),
                              "Video (2)")

    def test_a_missing_directory_has_nothing_to_collide_with(self):
        self.assertEqual(
            filenames.deduplicate_base_name(os.path.join("nope", "nowhere"), "Video"),
            "Video")


class WithPlaylistIndexTest(unittest.TestCase):
    def test_pads_to_the_width_of_the_total(self):
        self.assertEqual(filenames.with_playlist_index("Song", 3, 42), "03 - Song")
        self.assertEqual(filenames.with_playlist_index("Song", 7, 9), "7 - Song")
        self.assertEqual(filenames.with_playlist_index("Song", 5, 100), "005 - Song")

    def test_a_zero_total_still_produces_a_usable_prefix(self):
        self.assertEqual(filenames.with_playlist_index("Song", 1, 0), "1 - Song")


class ResolveFilenameBaseTest(unittest.TestCase):
    def test_sanitizes_numbers_and_deduplicates_in_that_order(self):
        with tempfile.TemporaryDirectory() as directory:
            base = playlist.resolve_filename_base(directory, 'Track: One/Two', 2, 10)
            self.assertEqual(base, "02 - Track_ One_Two")

            open(os.path.join(directory, base + ".mp4"), "w").close()
            self.assertEqual(
                playlist.resolve_filename_base(directory, 'Track: One/Two', 2, 10),
                "02 - Track_ One_Two (1)")

    def test_the_playlist_number_survives_max_path_truncation(self):
        # The number is applied before trimming, so trimming can never cut it off.
        with tempfile.TemporaryDirectory() as directory:
            base = playlist.resolve_filename_base(directory, "y" * 400, 4, 50)
            self.assertTrue(base.startswith("04 - "))

    def test_a_single_video_gets_no_number(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(playlist.resolve_filename_base(directory, "Just One"),
                              "Just One")


if __name__ == "__main__":
    unittest.main()
