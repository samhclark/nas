# ABOUTME: Locks the disposable exact-image Immich backup VM smoke boundary.

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
RUNNER = (REPO / "scripts/run-immich-backup-vm-smoke.sh").read_text()
PODMAN_WRAPPER = (REPO / "scripts/bcvk-bin/podman").read_text()
FIXTURE = (REPO / "tests/immich-backup-vm-smoke.sh").read_text()
MAKEFILE = (REPO / "Makefile").read_text()


class ImmichBackupVmSmokeSafetyTests(unittest.TestCase):
    def test_runner_is_exact_image_and_transient(self) -> None:
        self.assertIn("local_image='nas/bootc:stable'", RUNNER)
        self.assertIn("imported_image='localhost/nas/bootc:stable'", RUNNER)
        self.assertIn('docker-daemon:${local_image}', RUNNER)
        self.assertIn('containers-storage:${imported_image}', RUNNER)
        self.assertIn('[[ "${source_image_id}" == "${target_image_id}" ]]', RUNNER)
        for required in (
            "bcvk_bin",
            "--network=none",
            "--mount-disk-file",
            "--ro-bind",
            "--karg=selinux=1",
            "--karg=enforcing=0",
            "--execute=/run/virtiofs-mnt-seed/immich-backup-vm-smoke.sh",
            "--output=journal",
        ):
            with self.subTest(required=required):
                self.assertIn(required, RUNNER)

        for forbidden in ("/dev/sd", "/dev/nvme", "-netdev", "-virtfs", "-fsdev"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, RUNNER)
        self.assertNotRegex(RUNNER, re.compile(r"^\s*(?:rm|rmdir)\b", re.MULTILINE))
        self.assertIn("before_scratch_hash", RUNNER)
        self.assertIn("after_scratch_hash", RUNNER)
        self.assertIn("restic_archive_is_exact", RUNNER)
        self.assertIn('.Architecture == \\"amd64\\"', RUNNER)
        self.assertIn('.Os == \\"linux\\"', RUNNER)
        self.assertIn('readonly vm_name="nas-immich-backup-vm-smoke-${run_dir##*.}"', RUNNER)
        self.assertIn("artifacts retained", RUNNER)

    def test_fixture_is_networkless_and_has_no_production_authority(self) -> None:
        self.assertIn("--network=none", RUNNER)
        self.assertIn("/dev/tcp/1.1.1.1/443", FIXTURE)
        self.assertIn("--foreground --signal=TERM --kill-after=20s 900s", RUNNER)
        self.assertIn("bcvk-bin", RUNNER)
        self.assertIn("--dns=", PODMAN_WRAPPER)
        self.assertIn("/usr/bin/podman", PODMAN_WRAPPER)
        for forbidden in (
            "secrets.sops",
            "/var/lib/nas-secrets",
            "/dev/nvme",
            "/dev/sda",
            "cryptsetup",
            "b2-real",
            "tank/production",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, FIXTURE.lower())
        self.assertIn("zpool create -f", FIXTURE)
        self.assertIn('[[ "$(getenforce)" == Permissive ]]', FIXTURE)
        self.assertIn("virtio-scratch", FIXTURE)
        self.assertIn("fixture-only-restic-password", FIXTURE)
        self.assertIn("intentionally-missing-host-vm-taps.tsv", FIXTURE)
        self.assertIn("image_import_status", FIXTURE)
        self.assertIn("podman images --digests --no-trunc", FIXTURE)
        self.assertIn("expected_inventory", FIXTURE)
        self.assertIn("NAS_IMMICH_BACKUP_VM_SMOKE_PASS", FIXTURE)

    def test_fixture_checks_the_local_recovery_contract(self) -> None:
        for required in (
            "chown 51130:51130",
            "chmod 0750",
            "gzip -c",
            "setfattr -n user.nas_backup_vm_smoke",
            "semanage fcontext -a -t container_file_t -r s0",
            "matchpathcon -V",
            "ls -Zd",
            "Validated latest restic snapshot content:",
            "expected_files=3",
            "expected_bytes=$(stat -c %s",
            "gzip -t",
            'DF_BIN="${FIXTURE_DF}"',
            "nas-backup-immich init",
            "created restic repository",
            "local-success",
            "remote-success",
            "nas_backup_last_run_success{application=\"immich\"} 0",
            "restore --no-lock",
            "--exclude-xattr=security.selinux latest --target /restore",
            "--cap-add=CHOWN",
            "--cap-add=FOWNER",
            "--cap-add=DAC_OVERRIDE",
            "--cap-add=DAC_READ_SEARCH",
            'stat -c %u:%g "${restored_nested}"',
            "getfattr --only-values -n user.nas_backup_vm_smoke",
        ):
            with self.subTest(required=required):
                self.assertIn(required, FIXTURE)

    def test_make_targets_fixture_validation_and_smoke(self) -> None:
        self.assertIn("smoke-immich-backup-vm: deps-immich-backup-vm", MAKEFILE)
        self.assertIn(
            "deps-immich-backup-vm: deps-check-podman deps-check-jq deps-check-docker deps-check-skopeo",
            MAKEFILE,
        )
        self.assertIn("run-immich-backup-vm-smoke.sh", MAKEFILE)


if __name__ == "__main__":
    unittest.main()
