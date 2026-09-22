"""Mirrors Gravity's tests/downloads/YtDlpFormatSelectorTest.cpp for the Python port."""

import unittest

from gravity_cli import format_selector


class FormatSelectorTest(unittest.TestCase):
    def test_best_selects_separate_streams_with_a_combined_fallback(self):
        self.assertEqual(format_selector.format_selector_for_quality(format_selector.BEST),
                          "bestvideo*+bestaudio/best")

    def test_each_height_capped_preset_caps_both_halves_of_the_selector(self):
        for preset, height in ((format_selector.P2160, 2160), (format_selector.P1440, 1440),
                                (format_selector.P1080, 1080), (format_selector.P720, 720),
                                (format_selector.P480, 480)):
            selector = format_selector.format_selector_for_quality(preset)
            self.assertEqual(
                selector,
                f"bestvideo[height<={height}]+bestaudio/best[height<={height}]")

    def test_audio_only_never_selects_video(self):
        # "bestaudio/best" would also pass a naive substring check while still being able
        # to hand back a full video file, so the assertion is on the exact string.
        selector = format_selector.format_selector_for_quality(format_selector.AUDIO_ONLY)
        self.assertEqual(selector, "bestaudio")
        self.assertNotIn("video", selector)
        self.assertNotIn("/best", selector)

    def test_an_unknown_preset_falls_back_to_best(self):
        self.assertEqual(format_selector.format_selector_for_quality("NONSENSE"),
                          "bestvideo*+bestaudio/best")

    def test_every_preset_has_a_label_and_a_selector(self):
        for preset in format_selector.PRESETS:
            self.assertIn(preset, format_selector.PRESET_LABELS)
            self.assertTrue(format_selector.format_selector_for_quality(preset))


if __name__ == "__main__":
    unittest.main()
