"""C-01 safety regression cases. Run by operator in an isolated environment.

Never exercises real ClickMeeting endpoints.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import os
import sys
import tempfile
import types
import unittest

# Production app_cfg.py is deliberately not stored in Git.
sys.modules.setdefault("app_cfg", types.ModuleType("app_cfg"))
from run import recording_target_collision, target_path, verify_delete_retry_copy  # noqa: E402


def sample_recording(root: Path):
    started = datetime(2026, 10, 17, 13, 45, 10, tzinfo=timezone.utc)
    recording = SimpleNamespace(
        local_id=10,
        local_started=started,
        recording_name="Lecture",
        expected_size=6,
        local_path=None,
    )
    path = target_path(recording, root)
    return recording, path


class DeleteRetrySafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.recording, self.path = sample_recording(self.root)
        self.path.parent.mkdir(parents=True)

    def test_missing_local_copy_blocks_delete(self):
        self.recording.local_path = str(self.path)
        self.assertIn("missing", verify_delete_retry_copy(self.recording, self.root))

    def test_missing_persisted_path_blocks_delete(self):
        self.assertIn("missing recorded local_path", verify_delete_retry_copy(self.recording, self.root))

    def test_truncated_local_copy_blocks_delete(self):
        self.path.write_bytes(b"abc")
        self.recording.local_path = str(self.path)
        self.assertIn("size mismatch", verify_delete_retry_copy(self.recording, self.root))

    def test_valid_copy_allows_delete_precondition(self):
        self.path.write_bytes(b"abcdef")
        self.recording.local_path = str(self.path)
        self.assertIsNone(verify_delete_retry_copy(self.recording, self.root))

    def test_wrong_stored_path_blocks_delete(self):
        self.path.write_bytes(b"abcdef")
        self.recording.local_path = str(self.root / "elsewhere.mp4")
        self.assertIn("differs", verify_delete_retry_copy(self.recording, self.root))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_blocks_delete(self):
        real = self.root / "real.mp4"
        real.write_bytes(b"abcdef")
        self.path.symlink_to(real)
        self.recording.local_path = str(self.path)
        self.assertIn("symbolic link", verify_delete_retry_copy(self.recording, self.root))


class CollisionTests(unittest.TestCase):
    def test_same_name_and_local_time_from_other_recording_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            recording, path = sample_recording(Path(directory))
            store = SimpleNamespace(get_colliding_recordings=lambda pk, timestamp: [(11, "Lecture", recording.local_started.replace(tzinfo=None), None)])
            self.assertIn("#11", recording_target_collision(store, recording, path))

    def test_different_name_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            recording, path = sample_recording(Path(directory))
            store = SimpleNamespace(get_colliding_recordings=lambda pk, timestamp: [(11, "Other", recording.local_started.replace(tzinfo=None), None)])
            self.assertIsNone(recording_target_collision(store, recording, path))


if __name__ == "__main__":
    unittest.main()
