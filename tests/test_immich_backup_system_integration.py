# ABOUTME: Verifies the host integration contract for encrypted Immich backups.

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quadletgen.compiler import compile_fleet
from quadletgen.model import ConfigError, Fleet
from quadletgen.parser import load_host_secret_consumers
from tests.quadlet_test_support import OVERLAY, REPO, current_fleet


BACKUP_SECRET_NAMES = (
    "immich-backup-restic-password",
    "immich-backup-b2-key-id",
    "immich-backup-b2-application-key",
    "immich-backup-b2-bucket",
    "immich-backup-b2-s3-endpoint",
)


class ImmichBackupSystemIntegrationTests(unittest.TestCase):
    def test_host_secret_consumer_is_typed_and_root_only(self):
        fleet = current_fleet()
        consumer = next(
            item
            for item in fleet.host_secret_consumers
            if item.name == "immich-backup"
        )
        self.assertEqual(consumer.secrets, BACKUP_SECRET_NAMES)

        artifacts = {
            artifact.path: artifact.content for artifact in compile_fleet(fleet)
        }
        manifest = artifacts[Path("usr/share/nas/fleet/secrets.tsv")]
        for secret in BACKUP_SECRET_NAMES:
            self.assertIn(f"immich-backup\troot\t{secret}\n", manifest)
        self.assertNotIn("_nas_immich", "\n".join(
            line for line in manifest.splitlines() if "immich-backup\t" in line
        ))

    def test_host_secret_schema_rejects_unknown_and_empty_declarations(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            fleet_file = Path(directory_name) / "_fleet.toml"
            fleet_file.write_text(
                "[[host-secret-consumers]]\n"
                'name = "backup"\n'
                "secrets = []\n"
            )
            consumers = load_host_secret_consumers(fleet_file)
            with self.assertRaisesRegex(ConfigError, "at least one secret"):
                Fleet.build(
                    current_fleet().services,
                    host_secret_consumers=consumers,
                )

            fleet_file.write_text(
                "[[host-secret-consumers]]\n"
                'name = "backup"\n'
                'secrets = ["token"]\n'
                'owner = "someone"\n'
            )
            with self.assertRaisesRegex(ConfigError, "unknown keys: owner"):
                load_host_secret_consumers(fleet_file)

    def test_daily_and_weekly_timers_use_fixed_chicago_wall_clock(self):
        daily = (
            OVERLAY / "etc/systemd/system/nas-backup-immich.timer"
        ).read_text()
        weekly = (
            OVERLAY / "etc/systemd/system/nas-maintain-immich-backup.timer"
        ).read_text()
        self.assertIn(
            "OnCalendar=*-*-* 04:00:00 America/Chicago",
            daily,
        )
        self.assertIn("Persistent=true", daily)
        self.assertNotIn("RandomizedDelaySec", daily)
        self.assertIn(
            "OnCalendar=Sun *-*-* 06:00:00 America/Chicago",
            weekly,
        )
        self.assertIn("Persistent=true", weekly)

    def test_services_preserve_podman_and_zfs_access_but_lower_priority(self):
        for name, command in (
            ("nas-backup-immich.service", "run"),
            ("nas-maintain-immich-backup.service", "maintain"),
        ):
            with self.subTest(name=name):
                unit = (OVERLAY / "etc/systemd/system" / name).read_text()
                self.assertIn(
                    f"ExecStart=/usr/local/bin/nas-backup-immich {command}",
                    unit,
                )
                self.assertIn("Requires=zfs.target sops-distribute-secrets.service", unit)
                self.assertIn("nas-prepare-immich-server-storage.service", unit)
                self.assertIn("Nice=10", unit)
                self.assertIn("CPUWeight=20", unit)
                self.assertIn("IOWeight=20", unit)
                self.assertIn("IOSchedulingClass=idle", unit)
                self.assertIn("ProtectSystem=strict", unit)
                self.assertIn("ProtectHome=yes", unit)
                self.assertIn("/var/lib/nas-backups", unit)
                self.assertIn("/var/lib/containers", unit)
                self.assertNotIn("PrivateDevices=yes", unit)
                self.assertNotIn("PrivateNetwork=yes", unit)
                self.assertNotIn("CapabilityBoundingSet=", unit)

    def test_image_enables_backup_timers(self):
        containerfile = (REPO / "Containerfile").read_text()
        self.assertIn("nas-prepare-immich-backup-storage.service", containerfile)
        self.assertIn("nas-backup-immich.timer", containerfile)
        self.assertIn("nas-maintain-immich-backup.timer", containerfile)

    def test_backup_repository_has_persistent_selinux_policy_and_boot_repair(self):
        containerfile = (REPO / "Containerfile").read_text()
        self.assertIn(
            'semanage fcontext -a -t container_file_t -r s0',
            containerfile,
        )
        self.assertIn(
            '"/var/lib/nas-backups/immich/restic(/.*)?"',
            containerfile,
        )
        tmpfiles = (
            OVERLAY / "usr/lib/tmpfiles.d/nas-backups.conf"
        ).read_text()
        self.assertIn(
            "z /var/lib/nas-backups/immich/restic 0700 root root -",
            tmpfiles,
        )
        self.assertNotIn("z /var/lib/nas-backups 0700", tmpfiles)
        self.assertNotIn("z /var/lib/nas-backups/immich/state", tmpfiles)

    def test_backup_operations_reconcile_active_selinux_policy_without_recursive_relabel(self):
        preparer = (
            OVERLAY / "usr/local/bin/nas-prepare-immich-backup-storage"
        ).read_text()
        self.assertIn('"${MATCHPATHCON_BIN}" -n -- "${REPOSITORY}"', preparer)
        self.assertIn(
            '"${SEMANAGE_BIN}" fcontext -a -t container_file_t -r s0 "${target}"',
            preparer,
        )
        self.assertIn(
            '"${SEMANAGE_BIN}" fcontext -m -t container_file_t -r s0 "${target}"',
            preparer,
        )
        self.assertIn('"${RESTORECON_BIN}" -F -- "${REPOSITORY}"', preparer)
        self.assertNotIn('restorecon -F -R', preparer)

        runner = (
            OVERLAY / "usr/local/bin/nas-backup-immich"
        ).read_text()
        self.assertEqual(runner.count('"${PREPARE_STORAGE_BIN}"'), 3)

        preparation_unit = (
            OVERLAY
            / "etc/systemd/system/nas-prepare-immich-backup-storage.service"
        ).read_text()
        self.assertIn(
            "After=local-fs.target systemd-tmpfiles-setup.service",
            preparation_unit,
        )
        self.assertIn("WantedBy=multi-user.target", preparation_unit)
        for name in (
            "nas-backup-immich.service",
            "nas-maintain-immich-backup.service",
        ):
            unit = (OVERLAY / "etc/systemd/system" / name).read_text()
            self.assertIn("nas-prepare-immich-backup-storage.service", unit)
            self.assertNotIn("ReadWritePaths=/etc/selinux", unit)

    def test_backup_images_are_pinned_and_vm_launcher_is_hardened(self):
        runner = (
            OVERLAY / "usr/local/bin/nas-backup-immich"
        ).read_text()
        runtime = (
            OVERLAY / "usr/local/lib/nas-backup-runtime.sh"
        ).read_text()
        self.assertRegex(
            runner,
            r'RESTIC_IMAGE="docker\.io/restic/restic:0\.19\.1@sha256:[0-9a-f]{64}"',
        )
        self.assertRegex(
            runner,
            r'RCLONE_IMAGE="docker\.io/rclone/rclone:1\.74\.0@sha256:[0-9a-f]{64}"',
        )
        for argument in (
            "--runtime=krun",
            "--annotation=krun.cpus=2",
            "--annotation=krun.ram_mib=2048",
            "--read-only",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
        ):
            self.assertIn(argument, runtime)
        self.assertIn("network_arguments=(--network=none)", runtime)
        self.assertIn("--annotation=krun.use_passt=1", runtime)
        self.assertNotIn("--network=host", runtime)

    def test_alerts_cover_freshness_failures_integrity_and_capacity(self):
        alerts = (
            OVERLAY / "usr/share/nas/vmalert/alert-rules.yml"
        ).read_text()
        self.assertIn(
            'absent(nas_backup_last_success_timestamp_seconds{application="immich",destination="local"})',
            alerts,
        )
        self.assertIn(
            'absent(nas_backup_last_success_timestamp_seconds{application="immich",destination="b2"})',
            alerts,
        )
        self.assertIn(
            'nas_backup_last_run_success{application="immich"} == 0',
            alerts,
        )
        self.assertIn(
            'absent(nas_backup_integrity_last_success_timestamp_seconds{application="immich"})',
            alerts,
        )
        self.assertIn(
            'nas_backup_integrity_last_run_success{application="immich"} == 0',
            alerts,
        )
        self.assertIn(
            'nas_backup_filesystem_available_bytes{mount="/var"} < 107374182400',
            alerts,
        )
        self.assertIn(
            'nas_backup_filesystem_size_bytes{mount="/var"}) < 0.20',
            alerts,
        )


if __name__ == "__main__":
    unittest.main()
