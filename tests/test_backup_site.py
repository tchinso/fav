import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backup_site


class BackupPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repo"
        self.mirror = self.root / "mirror"
        self.repository.mkdir()
        self.mirror.mkdir()
        (self.repository / "index.html").write_text("last good backup", encoding="utf-8")
        (self.repository / "search-list.json").write_text("independent data", encoding="utf-8")
        (self.mirror / "index.html").write_text("verified new backup", encoding="utf-8")

    def test_replaces_only_managed_files_and_removes_obsolete_assets(self):
        old_asset = self.repository / "assets" / "obsolete.png"
        old_asset.parent.mkdir()
        old_asset.write_bytes(b"old")
        (self.repository / backup_site.MANIFEST).write_text(
            json.dumps({"files": {"index.html": {}, "assets/obsolete.png": {}}}), encoding="utf-8")
        backup_site.publish(self.mirror, self.repository)
        self.assertFalse(old_asset.exists())
        self.assertEqual((self.repository / "index.html").read_text(), "verified new backup")
        self.assertEqual((self.repository / "search-list.json").read_text(), "independent data")

    def test_collision_is_rejected_before_last_good_backup_changes(self):
        (self.mirror / "search-list.json").write_text("unrelated site file", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unsafe mirror path"):
            backup_site.publish(self.mirror, self.repository)
        self.assertEqual((self.repository / "index.html").read_text(), "last good backup")

    def test_manifest_cannot_delete_files_outside_repository(self):
        outside = self.root / "important.txt"
        outside.write_text("keep")
        (self.repository / backup_site.MANIFEST).write_text(
            json.dumps({"files": {"../important.txt": {}}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unsafe mirror path"):
            backup_site.publish(self.mirror, self.repository)
        self.assertEqual(outside.read_text(), "keep")
        self.assertEqual((self.repository / "index.html").read_text(), "last good backup")

    def test_failed_download_never_replaces_last_good_backup(self):
        output = self.root / "artifact"
        with patch.object(sys, "argv", ["backup_site.py", "--repository", str(self.repository),
                                       "--output", str(output), "--attempts", "1"]), \
             patch.object(backup_site, "download", side_effect=ValueError("missing font")):
            with self.assertRaises(SystemExit):
                backup_site.main()
        self.assertEqual((self.repository / "index.html").read_text(), "last good backup")
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
