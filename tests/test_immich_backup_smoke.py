# ABOUTME: Keeps the opt-in encrypted backup smoke fixture aligned with production.

from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
RUNNER = (REPO / "overlay-root/usr/local/bin/nas-backup-immich").read_text()
SMOKE = (REPO / "scripts/smoke-immich-backup.py").read_text()
MAKEFILE = (REPO / "Makefile").read_text()
SPEC = importlib.util.spec_from_file_location(
    "smoke_immich_backup", REPO / "scripts/smoke-immich-backup.py"
)
assert SPEC is not None and SPEC.loader is not None
SMOKE_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE_MODULE)


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
            "rclone sync",
            "--checksum",
            "exec rclone check",
            '"restore"',
            "PLAINTEXT in repository_file.read_bytes()",
            "total_files_processed",
            "total_bytes_processed",
            "--one-file-system/virtiofs",
            "nested original.jpg",
        ):
            self.assertIn(contract, SMOKE)
        self.assertNotIn('"--one-way"', SMOKE)
        self.assertNotIn('"--one-file-system"', SMOKE)
        self.assertIn('"--entrypoint=/bin/sh"', SMOKE)

    def test_krun_mode_is_explicit_and_production_shaped(self) -> None:
        self.assertIn('choices=("podman", "krun")', SMOKE)
        self.assertIn('"--runtime=krun"', SMOKE)
        self.assertIn('"--annotation=krun.cpus=2"', SMOKE)
        self.assertIn('"--annotation=krun.ram_mib=2048"', SMOKE)
        for argument in (
            '"--read-only"',
            '"--cap-drop=all"',
            '"--security-opt=no-new-privileges"',
            '"--network=none"',
        ):
            self.assertIn(argument, SMOKE)

        krun_prefix = SMOKE_MODULE.container_prefix("podman", "krun")
        self.assertIn("--runtime=krun", krun_prefix)
        self.assertIn("--read-only", krun_prefix)
        self.assertIn("--cap-drop=all", krun_prefix)
        self.assertIn("--network=none", krun_prefix)
        self.assertIn('"51130:51130"', SMOKE)
        self.assertIn('"0:0"', SMOKE)
        self.assertEqual(SMOKE.count('"--cap-add=DAC_READ_SEARCH"'), 1)

    def test_empty_snapshot_regression_is_a_hard_failure(self) -> None:
        nonempty = json.dumps(
            [{"summary": {"total_files_processed": 2, "total_bytes_processed": 10}}]
        )
        SMOKE_MODULE.require_complete_snapshot(nonempty, 10)

        empty = json.dumps(
            [{"summary": {"total_files_processed": 0, "total_bytes_processed": 0}}]
        )
        with self.assertRaisesRegex(RuntimeError, "--one-file-system/virtiofs"):
            SMOKE_MODULE.require_complete_snapshot(empty, 10)

        partial = json.dumps(
            [{"summary": {"total_files_processed": 1, "total_bytes_processed": 9}}]
        )
        with self.assertRaisesRegex(RuntimeError, "empty or partial snapshot"):
            SMOKE_MODULE.require_complete_snapshot(partial, 10)

    def test_make_exposes_the_opt_in_smoke(self) -> None:
        self.assertIn("smoke-immich-backup:", MAKEFILE)
        self.assertIn("scripts/smoke-immich-backup.py", MAKEFILE)
        self.assertIn("smoke-immich-backup-krun:", MAKEFILE)
        self.assertIn("--runtime=krun", MAKEFILE)


if __name__ == "__main__":
    unittest.main()
