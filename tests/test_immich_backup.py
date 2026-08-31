# ABOUTME: Behaviorally tests the isolated Immich backup orchestration.

from __future__ import annotations

import fcntl
import gzip
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "overlay-root/usr/local/bin/nas-backup-immich"
RUNTIME = REPO / "overlay-root/usr/local/lib/nas-backup-runtime.sh"
NOW = 1_788_192_000  # 2026-09-01T04:00:00Z


FAKE_DATE = """#!/bin/bash
if [[ "$*" == *%s* ]]; then printf '%s\n' "${FAKE_NOW}"; else printf '%s\n' 20260901T040000Z; fi
"""

FAKE_DF = """#!/bin/bash
printf ' Avail Size\n%s %s\n' "${FAKE_FREE}" "${FAKE_TOTAL}"
"""

FAKE_ZFS = """#!/bin/bash
set -euo pipefail
command=$1
shift
case "${command}" in
  list) cat "${FAKE_ZFS_STATE}" ;;
  snapshot)
    printf 'snapshot\t%s\n' "$1" >> "${FAKE_ZFS_LOG}"
    ;;
  destroy)
    printf 'destroy\t%s\n' "$1" >> "${FAKE_ZFS_LOG}"
    [[ "${FAKE_ZFS_FAIL_DESTROY:-0}" == 0 ]]
    ;;
  *) exit 2 ;;
esac
"""

FAKE_PODMAN = """#!/bin/bash
set -euo pipefail
printf '%q ' "$@" >> "${FAKE_PODMAN_LOG}"
printf '\n' >> "${FAKE_PODMAN_LOG}"
for argument in "$@"; do
  if [[ "${argument}" == --env-file=* && -n "${FAKE_ENV_CAPTURE:-}" ]]; then
    cat "${argument#--env-file=}" >> "${FAKE_ENV_CAPTURE}"
  fi
done
if [[ -n "${FAKE_PODMAN_FAIL_MATCH:-}" && "$*" == *"${FAKE_PODMAN_FAIL_MATCH}"* ]]; then
  exit "${FAKE_PODMAN_FAIL_STATUS:-1}"
fi
if [[ " $* " == *" snapshots --json --no-lock "* ]]; then
  printf '[]\n'
fi
if [[ " $* " == *" sync "* ]]; then
  printf '%s\n' '{"stats":{"bytes":12345}}' >&2
fi
"""


class ImmichBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REPO)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "library"
        self.backup = self.directory / "nas-backups" / "immich"
        self.secrets = self.directory / "secrets"
        self.metrics = self.directory / "metrics" / "backup.prom"
        self.lock = self.directory / "run" / "backup.lock"
        self.bin = self.directory / "bin"
        self.podman_log = self.directory / "podman.log"
        self.zfs_log = self.directory / "zfs.log"
        self.zfs_state = self.directory / "zfs-state"
        self.network_ready = self.directory / "network-ready"
        self.boot_id = self.directory / "boot-id"
        self.sys_class_net = self.directory / "sys-class-net"
        self.host_tap_manifest = self.directory / "host-vm-taps.tsv"
        self.network_lock = self.directory / "host-vm-tap-network.lock"
        for path in (self.source / "backups", self.secrets, self.bin):
            path.mkdir(parents=True)
        self.podman_log.write_text("")
        self.zfs_log.write_text("")
        self.zfs_state.write_text("")
        self.network_ready.write_text("test-boot-id\n")
        self.boot_id.write_text("test-boot-id\n")
        (self.sys_class_net / "krun-backup").mkdir(parents=True)
        self.host_tap_manifest.write_text(
            "# name\ttap\tguest\tmanaged-units\n"
            "immich-backup\tkrun-backup\t10.253.19.2/30\t"
            "nas-backup-immich.service,nas-maintain-immich-backup.service\n"
        )

        dump = self.source / "backups" / "immich-db.sql.gz"
        with gzip.open(dump, "wb") as output:
            output.write(b"CREATE TABLE asset (id uuid);\n")
        os.utime(dump, (NOW - 7200, NOW - 7200))
        (self.source / "original.jpg").write_bytes(b"photo")

        values = {
            "immich-backup-restic-password": "strong-restic-password",
            "immich-backup-b2-key-id": "001-key-id",
            "immich-backup-b2-application-key": "application-secret",
            "immich-backup-b2-bucket": "private-nas-backups",
            "immich-backup-b2-s3-endpoint": "https://s3.us-west-004.backblazeb2.com",
        }
        for name, value in values.items():
            (self.secrets / name).write_text(value)

        self.fake_date = self.write_executable("date", FAKE_DATE)
        self.fake_df = self.write_executable("df", FAKE_DF)
        self.fake_zfs = self.write_executable("zfs", FAKE_ZFS)
        self.fake_podman = self.write_executable("podman", FAKE_PODMAN)
        self.fake_prepare = self.write_executable(
            "prepare-storage", "#!/bin/bash\nexit 0\n"
        )
        self.environment = os.environ | {
            "NAS_BACKUP_SOURCE_ROOT": str(self.source),
            "NAS_BACKUP_ROOT": str(self.backup),
            "NAS_BACKUP_SECRET_DIR": str(self.secrets),
            "NAS_BACKUP_METRICS_FILE": str(self.metrics),
            "NAS_BACKUP_LOCK_FILE": str(self.lock),
            "NAS_BACKUP_RUNTIME_LIB": str(RUNTIME),
            "NAS_BACKUP_UNIQUE_TOKEN": "deadbeef",
            "DATE_BIN": str(self.fake_date),
            "DF_BIN": str(self.fake_df),
            "ZFS_BIN": str(self.fake_zfs),
            "PODMAN_BIN": str(self.fake_podman),
            "NAS_BACKUP_PREPARE_STORAGE_BIN": str(self.fake_prepare),
            "NAS_BACKUP_TUN_DEVICE": "/dev/null",
            "NAS_BACKUP_NETWORK_READY_FILE": str(self.network_ready),
            "NAS_BACKUP_BOOT_ID_FILE": str(self.boot_id),
            "NAS_BACKUP_SYS_CLASS_NET": str(self.sys_class_net),
            "NAS_BACKUP_HOST_TAP_MANIFEST": str(self.host_tap_manifest),
            "NAS_BACKUP_NETWORK_LOCK_FILE": str(self.network_lock),
            "FAKE_NOW": str(NOW),
            "FAKE_FREE": str(200 * 1024**3),
            "FAKE_TOTAL": str(500 * 1024**3),
            "FAKE_PODMAN_LOG": str(self.podman_log),
            "FAKE_ZFS_LOG": str(self.zfs_log),
            "FAKE_ZFS_STATE": str(self.zfs_state),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_executable(self, name: str, content: str) -> Path:
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)
        return path

    def invoke(self, command: str, **environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [RUNNER, command],
            env=self.environment | environment,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def invocations(self) -> list[str]:
        return self.podman_log.read_text().splitlines()

    def test_run_validates_then_backs_up_checks_and_mirrors(self) -> None:
        result = self.invoke("run")

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.invocations()
        self.assertEqual(len(calls), 4)
        self.assertIn("backup /source", calls[0])
        self.assertIn("--network=none", calls[0])
        self.assertIn("--read-only", calls[0])
        self.assertIn("--cap-drop=all", calls[0])
        self.assertIn("restic:0.19.1@sha256", calls[0])
        self.assertIn("check", calls[1])
        self.assertIn("sync /repository", calls[2])
        self.assertIn("b2:private-nas-backups/immich/restic/", calls[2])
        self.assertIn("--config=/dev/null", calls[2])
        self.assertIn("--delete-after", calls[2])
        self.assertIn("--bwlimit=5Mi", calls[2])
        self.assertIn("krun.tap_name=krun-backup", calls[2])
        self.assertIn("--network=host", calls[2])
        self.assertIn("--dns=100.100.100.100", calls[2])
        self.assertIn("--device=/dev/null", calls[2])
        self.assertIn("io.samhclark.nas.host-vm-tap=immich-backup", calls[2])
        self.assertNotIn("krun.use_passt", calls[2])
        self.assertIn("--pull=missing", calls[2])
        self.assertIn("check /repository", calls[3])
        self.assertIn("b2:private-nas-backups/immich/restic/", calls[3])
        self.assertIn("--config=/dev/null", calls[3])
        self.assertNotIn("--one-way", calls[3])
        self.assertEqual(
            self.zfs_log.read_text().splitlines(),
            [
                "snapshot\ttank/immich-server/library@nas-backup-immich-20260901T040000Z-deadbeef",
                "destroy\ttank/immich-server/library@nas-backup-immich-20260901T040000Z-deadbeef",
            ],
        )
        metrics = self.metrics.read_text()
        self.assertIn('destination="local"} 1788192000', metrics)
        self.assertIn('destination="b2"} 1788192000', metrics)
        self.assertIn("nas_backup_last_run_success{application=\"immich\"} 1", metrics)
        self.assertIn('destination="b2"} 12345', metrics)

    def test_credentials_never_enter_argv_and_each_vm_gets_only_its_inputs(self) -> None:
        result = self.invoke("run")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.invocations()
        all_arguments = "\n".join(calls)
        self.assertNotIn("application-secret", all_arguments)
        self.assertNotIn("strong-restic-password", all_arguments)
        self.assertNotIn("001-key-id", all_arguments)
        self.assertNotIn("original.jpg", all_arguments)
        self.assertNotIn(".rclone-env", calls[0])
        self.assertNotIn("--config=/dev/null", calls[0])
        self.assertNotIn("restic-password", calls[2])
        self.assertNotIn(str(self.source), calls[2])
        self.assertNotIn("krun.use_passt", calls[0])
        self.assertNotIn("krun.tap_name", calls[0])
        self.assertNotIn("--dns=", calls[0])
        self.assertNotIn("--device=", calls[0])

    def test_restic_partial_backup_status_three_prevents_verification_and_replication(self) -> None:
        result = self.invoke(
            "run", FAKE_PODMAN_FAIL_MATCH="backup", FAKE_PODMAN_FAIL_STATUS="3"
        )

        self.assertEqual(result.returncode, 3)
        self.assertEqual(len(self.invocations()), 1)
        self.assertIn("destroy\t", self.zfs_log.read_text())
        self.assertIn("nas_backup_last_run_success{application=\"immich\"} 0", self.metrics.read_text())

    def test_failed_local_check_prevents_replication_but_keeps_local_success_old(self) -> None:
        state = self.backup / "state"
        state.mkdir(parents=True)
        (state / "local-success").write_text("123\n")
        result = self.invoke("run", FAKE_PODMAN_FAIL_MATCH="check")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.invocations()), 2)
        self.assertEqual((state / "local-success").read_text(), "123\n")
        self.assertFalse((state / "remote-success").exists())

    def test_remote_compare_failure_preserves_new_local_and_old_remote_success(self) -> None:
        state = self.backup / "state"
        state.mkdir(parents=True)
        (state / "remote-success").write_text("456\n")
        # Match the rclone comparison without matching the local restic check.
        result = self.invoke("run", FAKE_PODMAN_FAIL_MATCH="check /repository")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((state / "local-success").read_text(), f"{NOW}\n")
        self.assertEqual((state / "remote-success").read_text(), "456\n")

    def test_stale_or_invalid_dump_fails_before_snapshot_or_vm(self) -> None:
        dump = self.source / "backups" / "immich-db.sql.gz"
        os.utime(dump, (NOW - 27 * 3600, NOW - 27 * 3600))
        stale = self.invoke("run")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("26-hour", stale.stderr)
        self.assertEqual(self.invocations(), [])
        self.assertEqual(self.zfs_log.read_text(), "")

        os.utime(dump, (NOW - 100, NOW - 100))
        dump.write_bytes(b"not gzip")
        invalid = self.invoke("run")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertEqual(self.invocations(), [])

    def test_capacity_floor_refuses_before_accessing_backup_inputs(self) -> None:
        result = self.invoke("run", FAKE_FREE=str(99 * 1024**3))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("at least", result.stderr)
        self.assertEqual(self.invocations(), [])

    def test_sentinel_b2_value_fails_before_any_network_call(self) -> None:
        (self.secrets / "immich-backup-b2-key-id").write_text(
            "PENDING_OPERATOR_CONFIGURATION"
        )
        result = self.invoke("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sentinel", result.stderr)
        self.assertEqual(self.invocations(), [])
        self.assertNotIn("PENDING_OPERATOR_CONFIGURATION", result.stderr)

    def test_b2_region_is_derived_for_rclone_and_remote_restic(self) -> None:
        capture = self.directory / "captured-environments"
        capture.write_text("")
        result = self.invoke("maintain", FAKE_ENV_CAPTURE=str(capture))
        self.assertEqual(result.returncode, 0, result.stderr)
        environments = capture.read_text()
        self.assertIn("RCLONE_CONFIG_B2_REGION=us-west-004", environments)
        self.assertIn(
            "RCLONE_CONFIG_B2_LOCATION_CONSTRAINT=us-west-004", environments
        )
        self.assertIn("AWS_DEFAULT_REGION=us-west-004", environments)

    def test_noncanonical_b2_endpoint_fails_before_any_network_call(self) -> None:
        (self.secrets / "immich-backup-b2-s3-endpoint").write_text(
            "https://example.com"
        )
        result = self.invoke("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regional form", result.stderr)
        self.assertEqual(self.invocations(), [])

    def test_snapshot_destroy_failure_changes_successful_run_to_failure(self) -> None:
        result = self.invoke("run", FAKE_ZFS_FAIL_DESTROY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "nas_backup_last_run_success{application=\"immich\"} 0",
            self.metrics.read_text(),
        )

    def test_only_exact_old_unheld_uncloned_staging_snapshots_are_removed(self) -> None:
        old = NOW - 49 * 3600
        self.zfs_state.write_text(
            f"tank/immich-server/library@nas-backup-immich-20260829T010000Z-aabbccdd\t{old}\t0\t-\n"
            f"tank/immich-server/library@nas-backup-immich-20260829T010000Z-bbccddee\t{old}\t1\t-\n"
            f"tank/immich-server/library@nas-backup-immich-20260829T010000Z-ccddeeff\t{old}\t0\ttank/clone\n"
            f"tank/immich-server/library@nas-backup-immich-manual\t{old}\t0\t-\n"
        )
        result = self.invoke("run")
        self.assertEqual(result.returncode, 0, result.stderr)
        destroys = [line for line in self.zfs_log.read_text().splitlines() if line.startswith("destroy")]
        self.assertEqual(len(destroys), 2)  # one stale plus the current snapshot
        self.assertTrue(any("aabbccdd" in line for line in destroys))
        self.assertFalse(any("bbccddee" in line or "ccddeeff" in line or "manual" in line for line in destroys))

    def test_maintain_prunes_then_checks_mirrors_and_checks_remote_subset(self) -> None:
        result = self.invoke("maintain")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.invocations()
        self.assertIn("forget --keep-daily=14 --keep-weekly=8 --keep-monthly=12 --keep-yearly=3 --prune", calls[0])
        self.assertIn("--network=none", calls[1])
        self.assertIn("sync /repository", calls[2])
        self.assertIn("check /repository", calls[3])
        self.assertIn("--read-data-subset=", calls[4])
        self.assertIn("krun.tap_name=krun-backup", calls[4])
        self.assertIn("--network=host", calls[4])
        self.assertIn("--dns=100.100.100.100", calls[4])
        self.assertNotIn("krun.use_passt", calls[4])
        metrics = self.metrics.read_text()
        self.assertIn("nas_backup_integrity_last_run_success{application=\"immich\"} 1", metrics)
        self.assertIn(f"nas_backup_integrity_last_success_timestamp_seconds{{application=\"immich\"}} {NOW}", metrics)

    def test_failed_remote_integrity_check_removes_temporary_credentials(self) -> None:
        result = self.invoke(
            "maintain", FAKE_PODMAN_FAIL_MATCH="--read-data-subset="
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.backup.glob(".remote-restic-env.*")), [])

    def test_lock_contention_refuses_without_starting_a_vm(self) -> None:
        self.lock.parent.mkdir(parents=True)
        with self.lock.open("w") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.invoke("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another Immich backup", result.stderr)
        self.assertEqual(self.invocations(), [])

    def test_stale_network_readiness_prevents_outbound_vm(self) -> None:
        self.network_ready.write_text("previous-boot-id\n")
        result = self.invoke("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("readiness is stale", result.stderr)
        self.assertEqual(len(self.invocations()), 2)
        self.assertNotIn("rclone", "\n".join(self.invocations()))

    def test_missing_or_ambiguous_host_tap_manifest_prevents_outbound_vm(self) -> None:
        self.host_tap_manifest.unlink()
        missing = self.invoke("run")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("manifest is unavailable", missing.stderr)
        self.assertEqual(len(self.invocations()), 2)

        self.podman_log.write_text("")
        self.host_tap_manifest.write_text(
            "immich-backup\tkrun-backup\t10.253.19.2/30\tfirst.service\n"
            "immich-backup\tkrun-other\t10.253.20.2/30\tsecond.service\n"
        )
        ambiguous = self.invoke("run")
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertIn("exactly one manifest entry", ambiguous.stderr)
        self.assertEqual(len(self.invocations()), 2)

    def test_init_never_reinitializes_nonempty_repository(self) -> None:
        repository = self.backup / "restic"
        repository.mkdir(parents=True)
        (repository / "config").write_text("existing")
        result = self.invoke("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nonempty", result.stderr)
        self.assertEqual(self.invocations(), [])

    def test_init_resumes_first_mirror_of_runner_checked_empty_repository(self) -> None:
        repository = self.backup / "restic"
        state = self.backup / "state"
        repository.mkdir(parents=True)
        state.mkdir(parents=True)
        (repository / "config").write_text("existing")
        (state / "local-success").write_text(f"{NOW}\n")

        result = self.invoke("init")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resuming the first mirror", result.stdout)
        calls = self.invocations()
        self.assertEqual(len(calls), 4)
        self.assertIn("snapshots --json --no-lock", calls[0])
        self.assertIn("check", calls[1])
        self.assertIn("sync /repository", calls[2])
        self.assertIn("check /repository", calls[3])
        self.assertNotIn(" init", "\n".join(calls))


if __name__ == "__main__":
    unittest.main()
