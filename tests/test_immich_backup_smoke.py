# ABOUTME: Keeps the opt-in encrypted backup smoke fixture aligned with production.

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
RUNNER = (REPO / "overlay-root/usr/local/bin/nas-backup-immich").read_text()
SMOKE = (REPO / "scripts/smoke-immich-backup.py").read_text()
MAKEFILE = (REPO / "Makefile").read_text()


class ImmichBackupSmokeContractTests(unittest.TestCase):
    def test_smoke_uses_the_production_pinned_images(self) -> None:
        production_images = re.findall(
            r'readonly (?:RESTIC|RCLONE)_IMAGE="([^"]+)"', RUNNER
        )
        self.assertEqual(len(production_images), 2)
        for image in production_images:
            self.assertIn(image, SMOKE)

    def test_smoke_covers_encryption_xattrs_mirror_check_and_restore(self) -> None:
        for contract in (
            "os.setxattr",
            "os.getxattr",
            '"sync"',
            '"--checksum"',
            '"restore"',
            "PLAINTEXT in repository_file.read_bytes()",
        ):
            self.assertIn(contract, SMOKE)
        self.assertNotIn('"--one-way"', SMOKE)

    def test_make_exposes_the_opt_in_smoke(self) -> None:
        self.assertIn("smoke-immich-backup:", MAKEFILE)
        self.assertIn("scripts/smoke-immich-backup.py", MAKEFILE)


if __name__ == "__main__":
    unittest.main()
