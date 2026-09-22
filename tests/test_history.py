"""Covers the download-history ring buffer and its atomic write."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gravity_cli import history, storage


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "history.json"
        patcher = mock.patch.object(history, "history_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_absent_file_reads_as_an_empty_history(self):
        self.assertEqual(history.load(), [])

    def test_a_recorded_download_round_trips(self):
        self.assertTrue(history.record(
            url="https://www.youtube.com/watch?v=abc",
            title="A Video",
            kind="video",
            quality="1080P",
            status=history.COMPLETED,
            output_path="C:\\Videos\\A Video.mp4",
        ))

        entries = history.load()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "A Video")
        self.assertEqual(entries[0]["status"], "COMPLETED")
        self.assertEqual(entries[0]["outputPath"], "C:\\Videos\\A Video.mp4")
        self.assertTrue(entries[0]["timestamp"])
        self.assertNotIn("error", entries[0])

    def test_newest_entries_come_first(self):
        for title in ("First", "Second", "Third"):
            history.record("https://example.com", title, "video", "BEST",
                            history.COMPLETED)
        self.assertEqual([e["title"] for e in history.load()],
                          ["Third", "Second", "First"])

    def test_a_failure_keeps_its_error_and_source(self):
        history.record("https://example.com", "Dead Video", "playlist-entry", "BEST",
                        history.FAILED, error="E_VIDEO_PRIVATE: Private video",
                        source="My Playlist")
        entry = history.load()[0]
        self.assertEqual(entry["status"], "FAILED")
        self.assertEqual(entry["error"], "E_VIDEO_PRIVATE: Private video")
        self.assertEqual(entry["source"], "My Playlist")

    def test_the_ring_buffer_is_bounded(self):
        seed = [{"title": f"Old {n}"} for n in range(history.MAX_ENTRIES)]
        storage.write_json_atomic(self.path, seed)

        history.record("https://example.com", "Newest", "video", "BEST",
                        history.COMPLETED)

        entries = history.load()
        self.assertEqual(len(entries), history.MAX_ENTRIES)
        self.assertEqual(entries[0]["title"], "Newest")
        self.assertNotIn("Old 499", [e.get("title") for e in entries])

    def test_a_corrupt_file_reads_as_empty_rather_than_raising(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(history.load(), [])

    def test_the_write_leaves_no_temporary_files_behind(self):
        history.record("https://example.com", "A Video", "video", "BEST",
                        history.COMPLETED)
        leftovers = [p.name for p in self.path.parent.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        json.loads(self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
