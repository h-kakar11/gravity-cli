"""Verifies downloader.py --selftest against docs/ipc-contract.md, using only unittest
(no pytest, no extra deps -- this suite runs outside the venv that has yt_dlp)."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

DOWNLOADER_SCRIPT = Path(__file__).resolve().parents[1] / "vendor" / "downloader.py"


class SelfTestProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            [sys.executable, str(DOWNLOADER_SCRIPT), "--selftest"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        cls.result = result
        cls.lines = [line for line in result.stdout.splitlines() if line.strip()]
        cls.events = [json.loads(line) for line in cls.lines]

    def test_exits_zero(self):
        self.assertEqual(self.result.returncode, 0, msg=self.result.stderr)

    def test_every_line_is_a_single_json_object(self):
        for line in self.lines:
            parsed = json.loads(line)
            self.assertIsInstance(parsed, dict)

    def test_event_sequence_is_metadata_then_progress_then_completed(self):
        names = [event["event"] for event in self.events]
        self.assertEqual(names[0], "metadata")
        self.assertEqual(names[-1], "completed")
        middle = names[1:-1]
        self.assertGreaterEqual(len(middle), 3)
        self.assertLessEqual(len(middle), 4)
        self.assertTrue(all(name == "progress" for name in middle))
        self.assertNotIn("error", names)

    def test_metadata_fields(self):
        metadata = self.events[0]["data"]
        self.assertIsInstance(metadata["title"], str)
        self.assertIn("duration", metadata)
        self.assertIn("playlistIndex", metadata)
        self.assertIn("playlistCount", metadata)

    def test_progress_fields_and_monotonicity(self):
        progress_events = [event["data"] for event in self.events[1:-1]]
        downloaded = [p["downloadedBytes"] for p in progress_events]
        etas = [p["etaSeconds"] for p in progress_events]

        for progress in progress_events:
            self.assertIsInstance(progress["downloadedBytes"], int)
            self.assertIsInstance(progress["statusMessage"], str)
            self.assertTrue(progress["statusMessage"])

        # non-decreasing downloadedBytes, strictly increasing in this canned sequence
        self.assertEqual(downloaded, sorted(downloaded))
        for earlier, later in zip(downloaded, downloaded[1:]):
            self.assertGreater(later, earlier)

        # non-increasing etaSeconds
        self.assertEqual(etas, sorted(etas, reverse=True))

    def test_completed_field(self):
        completed = self.events[-1]["data"]
        self.assertIsInstance(completed["outputPath"], str)
        self.assertTrue(completed["outputPath"])


if __name__ == "__main__":
    unittest.main()
