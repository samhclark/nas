# ABOUTME: Unit-tests command construction and ownership checks for Immich smoke tests.

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/smoke-immich-images.py"
SPEC = importlib.util.spec_from_file_location("smoke_immich_images", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
SMOKE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SMOKE
SPEC.loader.exec_module(SMOKE)


class ImmichSmokeUnitTests(unittest.TestCase):
    def test_postgresql_smoke_uses_the_production_storage_mount_target(self):
        service = SMOKE.service("immich-database")

        self.assertEqual(
            SMOKE.postgres_data_container_path(service),
            "/var/lib/postgresql",
        )

    def test_declared_identity_uses_matching_process_and_keep_id_mapping(self):
        service = SMOKE.service("immich-database")

        arguments = SMOKE.container_arguments(
            service,
            "postgres-smoke",
            process_identity=SMOKE.DECLARED_IDENTITY,
        )

        self.assertIn("--user=1000:1000", arguments)
        self.assertIn("--userns=keep-id:uid=1000,gid=1000", arguments)

    def test_guest_root_emulation_retains_declared_keep_id_mapping(self):
        service = SMOKE.service("immich-database")

        arguments = SMOKE.container_arguments(
            service,
            "postgres-smoke",
            process_identity=SMOKE.GUEST_ROOT_IDENTITY,
        )

        self.assertIn("--user=0:0", arguments)
        self.assertIn("--userns=keep-id:uid=1000,gid=1000", arguments)

    def test_valkey_guest_root_smoke_uses_the_declared_keep_id_mapping(self):
        service = SMOKE.service("immich-valkey")

        arguments = SMOKE.container_arguments(
            service,
            "valkey-smoke",
            process_identity=SMOKE.GUEST_ROOT_IDENTITY,
        )

        self.assertIn("--user=0:0", arguments)
        self.assertIn("--userns=keep-id:uid=1000,gid=1000", arguments)

    def test_ownership_check_accepts_new_files_owned_by_invoking_user(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data"
            path.mkdir()
            before = SMOKE.ownership_snapshot(path)
            child = path / "created-by-container"
            child.write_text("data")
            child.chmod(0o600)
            self.assertEqual(
                (child.stat().st_uid, child.stat().st_gid),
                (os.geteuid(), os.getegid()),
            )
            SMOKE.assert_host_ownership(path, before, "test data")

    def test_ownership_check_rejects_a_host_owner_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data"
            path.mkdir()
            before = SMOKE.ownership_snapshot(path)
            with mock.patch.object(SMOKE.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaisesRegex(RuntimeError, "changed host ownership"):
                    SMOKE.assert_host_ownership(path, before, "test data")


if __name__ == "__main__":
    unittest.main()
