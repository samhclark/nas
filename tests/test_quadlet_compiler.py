# ABOUTME: Tests compiler rendering against the repository fleet.

from __future__ import annotations

import stat
import unittest
from dataclasses import replace
from pathlib import Path

from quadletgen.compiler import Artifact, compile_fleet
from quadletgen.model import ConfigError, Fleet, KrunNetwork
from tests.quadlet_test_support import (
    GENERATED_PREFIX,
    OVERLAY,
    REPO,
    current_fleet,
)


class CompilerCharacterizationTests(unittest.TestCase):
    def test_compiler_exactly_reproduces_every_tracked_generated_artifact(self):
        artifacts = compile_fleet(current_fleet())
        compiled_paths = {artifact.path for artifact in artifacts}

        tracked_generated = set()
        for path in OVERLAY.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            first_two = path.read_text(errors="replace").splitlines()[:2]
            if any(line.startswith(GENERATED_PREFIX) for line in first_two):
                tracked_generated.add(path.relative_to(OVERLAY))
        tracked_generated.update({Path("etc/subuid"), Path("etc/subgid")})

        self.assertEqual(compiled_paths, tracked_generated)
        for artifact in artifacts:
            with self.subTest(path=artifact.path):
                path = OVERLAY / artifact.path
                self.assertEqual(artifact.content, path.read_text())
                executable = bool(path.stat().st_mode & stat.S_IXUSR)
                self.assertEqual(artifact.executable, executable)

    def test_current_fleet_is_typed_and_uid_ordered(self):
        fleet = current_fleet()

        self.assertEqual(len(fleet.services), 18)
        self.assertEqual(len(fleet.active_taps), 18)
        self.assertEqual(
            [service.host.uid for service in fleet.services],
            sorted(service.host.uid for service in fleet.services),
        )
        self.assertTrue(
            all(
                service.krun is not None
                and service.krun.network is KrunNetwork.TAP
                for service in fleet.active_taps
            )
        )

    def test_journald_log_driver_is_rendered_only_for_declared_services(self):
        artifacts = {
            artifact.path: artifact.content
            for artifact in compile_fleet(current_fleet())
        }
        expected_logged = {
            "alertmanager",
            "blackbox-exporter",
            "caddy",
            "garage",
            "grafana",
            "immich-database",
            "immich-machine-learning",
            "immich-server",
            "immich-valkey",
            "jellyfin",
            "jellyfin-exporter",
            "prowlarr",
            "radarr",
            "sabnzbd",
            "sonarr",
            "victoria-metrics",
            "vmalert",
        }
        for path, content in artifacts.items():
            if path.suffix != ".container":
                continue
            if path.stem in expected_logged:
                self.assertIn("LogDriver=journald\n", content)
            else:
                self.assertNotIn("LogDriver=", content)

    def test_artifact_paths_are_normalized_relative_overlay_paths(self):
        for path in (Path("../escape"), Path("/absolute"), Path("bad\\name")):
            with self.subTest(path=path):
                with self.assertRaises(ConfigError):
                    Artifact(path, "content")


class FleetManifestTests(unittest.TestCase):
    def setUp(self):
        self.fleet = current_fleet()
        self.artifacts = {
            artifact.path: artifact.content
            for artifact in compile_fleet(self.fleet)
        }

    def rows(self, path: str):
        return [
            line.split("\t")
            for line in self.artifacts[Path(path)].splitlines()
            if line and not line.startswith("#")
        ]

    def test_manifests_encode_account_and_active_tap_membership(self):
        account_units = [
            line
            for line in self.artifacts[
                Path("usr/share/nas/fleet/account-units.list")
            ].splitlines()
            if line and not line.startswith("#")
        ]
        tap_rows = self.rows("usr/share/nas/fleet/active-taps.tsv")

        self.assertEqual(
            account_units,
            sorted(
                f"ensure-nas-{service.host.slug}-account.service"
                for service in self.fleet.services
            ),
        )
        self.assertEqual(
            tap_rows,
            [
                [
                    service.tap_name,
                    f"user@{service.host.uid}.service",
                    f"ensure-nas-{service.host.slug}-account.service",
                ]
                for service in self.fleet.active_taps
            ],
        )

    def test_secret_and_asset_manifests_are_derived_from_typed_fields(self):
        secret_rows = self.rows("usr/share/nas/fleet/secrets.tsv")
        asset_rows = [
            [line]
            for line in self.artifacts[
                Path("usr/share/nas/fleet/assets.list")
            ].splitlines()
            if line and not line.startswith("#")
        ]

        expected_secrets = [
            [service.info.name, service.host.username, secret.name]
            for service in sorted(
                self.fleet.services,
                key=lambda item: item.source.name,
            )
            for secret in service.container.secrets
        ]
        if self.fleet.egress is not None:
            expected_secrets.append(
                ["mullvad", "root", self.fleet.egress.secret_name]
            )
        expected_assets = [
            [service.assets.path]
            for service in sorted(
                self.fleet.services,
                key=lambda item: item.assets.path if item.assets else "",
            )
            if service.assets is not None
        ]
        self.assertEqual(secret_rows, expected_secrets)
        self.assertEqual(asset_rows, expected_assets)

    def test_non_python_consumers_no_longer_parse_or_duplicate_the_fleet(self):
        containerfile = (REPO / "Containerfile").read_text()
        distributor = (
            OVERLAY / "usr/local/bin/sops-distribute-secrets.sh"
        ).read_text()
        diagnostics = (
            REPO / "scripts/collect-krun-network-diagnostics.sh"
        ).read_text()

        self.assertNotIn("COPY quadlets/", containerfile)
        self.assertIn("fleet/account-units.list", containerfile)
        self.assertIn("fleet/assets.list", containerfile)
        self.assertIn("fleet/shared-storage-paths.list", containerfile)
        self.assertIn(
            'semanage fcontext -a -t container_file_t -r s0 "${path}(/.*)?"',
            containerfile,
        )
        self.assertNotIn(
            'restorecon -F -R -- "${path}"',
            containerfile,
        )
        self.assertNotIn("ensure-nas-alertmanager-account.service \\", containerfile)
        self.assertIn("fleet/secrets.tsv", distributor)
        self.assertNotIn("read_quadlet_secret_rows", distributor)
        self.assertNotIn("QUADLET_DIR", distributor)
        self.assertIn("fleet/active-taps.tsv", diagnostics)
        self.assertNotIn("krun-51110 krun-51120", diagnostics)

    def test_selinux_labeling_has_one_owner_per_storage_class(self):
        containerfile = (REPO / "Containerfile").read_text()
        self.assertIn('awk "NF && !/^#/"', containerfile)
        self.assertIn('restorecon -F -R -- "${asset}"', containerfile)
        self.assertNotIn('restorecon -F -R "${image_assets[@]}"', containerfile)
        self.assertNotIn('/var/lib/grafana(/.*)?', containerfile)

        for service in self.fleet.services:
            script = self.artifacts[
                Path(
                    f"usr/local/bin/ensure-nas-{service.host.slug}-account.sh"
                )
            ]
            if service.assets is not None:
                self.assertNotIn(service.assets.path, script)
            self.assertNotIn("semanage fcontext", script)
            self.assertNotIn("restorecon", script)


class StartupPolicyTests(unittest.TestCase):
    def setUp(self):
        self.artifacts = {
            artifact.path: artifact.content
            for artifact in compile_fleet(current_fleet())
        }

    def test_current_services_use_typed_startup_policy_only(self):
        for toml_path in (REPO / "quadlets").glob("[!_]*.toml"):
            self.assertNotIn("[unit.extra]", toml_path.read_text())

        garage = self.artifacts[
            Path("etc/containers/systemd/users/51110/garage.container")
        ]
        jellyfin = self.artifacts[
            Path("etc/containers/systemd/users/51120/jellyfin.container")
        ]
        vmalert = self.artifacts[
            Path("etc/containers/systemd/users/51220/vmalert.container")
        ]
        self.assertIn(
            "nas-wait-for-readiness.sh marker "
            "/run/nas-storage/garage/ready 300 2 "
            "--path /var/lib/garage/meta --source tank/garage/meta "
            "--owner 51110:51110 --access rwx "
            "--path /var/lib/garage/data --source tank/garage/data "
            "--owner 51110:51110 --access rwx",
            garage,
        )
        self.assertIn(
            "nas-assert-tcp-ports-free.sh 3900 3902 3903",
            garage,
        )
        self.assertIn(
            "--path /var/lib/jellyfin/config --source tank/jellyfin/config "
            "--owner 51120:51120 --access rwx",
            jellyfin,
        )
        self.assertIn(
            "--path /var/lib/jellyfin/cache --source tank/jellyfin/cache "
            "--owner 51120:51120 --access rwx",
            jellyfin,
        )
        self.assertIn(
            "--path /var/zfs/tank/videos/data/media --source tank/videos "
            "--access rx",
            jellyfin,
        )
        self.assertIn(
            "nas-wait-for-readiness.sh http "
            "http://10.253.7.2:8428/-/healthy 300 2",
            vmalert,
        )
        for artifact in self.artifacts.values():
            self.assertNotIn("ExecStartPre=/usr/bin/bash -lc", artifact)

    def test_alertmanager_storage_is_not_duplicated_in_account_provisioning(self):
        tmpfiles = self.artifacts[
            Path("usr/lib/tmpfiles.d/nas-alertmanager-rootless.conf")
        ]
        self.assertNotIn("/var/lib/alertmanager", tmpfiles)
        manifest = self.artifacts[
            Path(
                "usr/share/nas/storage/"
                "alertmanager.storage-manifest"
            )
        ]
        self.assertIn("directory|/var/lib/alertmanager|0750", manifest)
        self.assertIn("directory|/var/lib/alertmanager/data|0750", manifest)
        alertmanager = self.artifacts[
            Path("etc/containers/systemd/users/51240/alertmanager.container")
        ]
        self.assertIn(
            "Volume=/var/lib/alertmanager/data:/alertmanager",
            alertmanager,
        )
        self.assertIn("/run/nas-storage/alertmanager/ready", alertmanager)


if __name__ == "__main__":
    unittest.main()
