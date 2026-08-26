# ABOUTME: Verifies the production Immich application is compiled from named
# component, endpoint, storage, secret, and startup contracts.

from __future__ import annotations

import unittest
from pathlib import Path

from quadletgen.compiler import compile_fleet
from tests.quadlet_test_support import OVERLAY, REPO, current_fleet


class ImmichDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fleet = current_fleet()
        cls.artifacts = {
            artifact.path: artifact.content
            for artifact in compile_fleet(cls.fleet)
        }

    def test_application_has_four_unique_production_roles(self):
        components = {
            service.info.role: service
            for service in self.fleet.services
            if service.info.application == "immich"
        }
        self.assertEqual(
            set(components),
            {"server", "database", "cache", "machine-learning"},
        )
        self.assertTrue(all(component.active_tap for component in components.values()))
        self.assertEqual(
            components["server"].container.image,
            "ghcr.io/immich-app/immich-server:v3.1.0@sha256:"
            "b434cb9287eea1471c9974845914d4dd328c9c2d652e446ed4930f99944f0ceb",
        )

    def test_server_combines_storage_secrets_and_named_dependencies(self):
        server = self.artifacts[
            Path("etc/containers/systemd/users/51130/immich-server.container")
        ]
        self.assertIn("UserNS=keep-id:uid=1000,gid=1000", server)
        self.assertIn("NoNewPrivileges=true", server)
        self.assertIn("DropCapability=NET_RAW", server)
        self.assertIn("Volume=/var/lib/immich/library:/data", server)
        self.assertIn("Volume=/var/lib/immich/thumbs:/data/thumbs", server)
        self.assertIn(
            "Volume=/var/lib/immich/encoded-video:/data/encoded-video",
            server,
        )
        self.assertIn(
            "Volume=/run/nas-secrets/immich-server/immich-db-password:"
            "/run/secrets/immich-db-password:ro,Z",
            server,
        )
        self.assertIn("tcp 10.253.11.2:5432 300 2", server)
        self.assertIn("tcp 10.253.12.2:6379 300 2", server)
        self.assertIn("for i in {1..300}", server)

    def test_valkey_applies_guest_overcommit_policy_before_dropping_privileges(self):
        valkey = self.artifacts[
            Path("etc/containers/systemd/users/51150/immich-valkey.container")
        ]
        self.assertIn(
            "Entrypoint=/usr/share/nas/immich-valkey/immich-valkey-entrypoint.sh",
            valkey,
        )
        self.assertIn("Exec=--port 6379", valkey)
        self.assertIn(
            "Volume=/usr/share/nas/immich-valkey:/usr/share/nas/immich-valkey:ro",
            valkey,
        )

        assets = self.artifacts[Path("usr/share/nas/fleet/assets.list")]
        self.assertIn("/usr/share/nas/immich-valkey", assets)

        entrypoint = (
            REPO
            / "overlay-root/usr/share/nas/immich-valkey/immich-valkey-entrypoint.sh"
        )
        self.assertTrue(entrypoint.stat().st_mode & 0o111)
        entrypoint_text = entrypoint.read_text()
        self.assertIn('printf \'1\\n\' >"${overcommit_path}"', entrypoint_text)
        self.assertIn("exec setpriv --reuid=1000 --regid=1000", entrypoint_text)

    def test_tini_is_a_subreaper_beneath_libkrun_guest_init(self):
        for uid, name in (
            (51130, "immich-server"),
            (51160, "immich-machine-learning"),
        ):
            with self.subTest(service=name):
                unit = self.artifacts[
                    Path(f"etc/containers/systemd/users/{uid}/{name}.container")
                ]
                self.assertIn("Environment=TINI_SUBREAPER=1", unit)

    def test_database_and_rebuildable_components_keep_distinct_storage(self):
        database = self.artifacts[
            Path("etc/containers/systemd/users/51140/immich-database.container")
        ]
        self.assertIn("User=1000:1000\n", database)
        self.assertIn("UserNS=keep-id:uid=1000,gid=1000", database)
        self.assertIn(
            "Entrypoint=/usr/share/nas/immich-database/"
            "immich-database-entrypoint.sh",
            database,
        )
        self.assertIn(
            "Exec=postgres -c config_file=/etc/postgresql/postgresql.conf",
            database,
        )
        self.assertIn(
            "Volume=/usr/share/nas/immich-database:"
            "/usr/share/nas/immich-database:ro",
            database,
        )
        self.assertIn("ShmSize=128m", database)
        self.assertIn("Environment=POSTGRES_INITDB_ARGS=--data-checksums", database)
        self.assertIn("Environment=DB_STORAGE_TYPE=HDD", database)

        assets = self.artifacts[
            Path("usr/share/nas/fleet/assets.list")
        ]
        self.assertIn("/usr/share/nas/immich-database", assets)

        entrypoint = (
            REPO
            / "overlay-root/usr/share/nas/immich-database/"
            "immich-database-entrypoint.sh"
        )
        self.assertTrue(entrypoint.stat().st_mode & 0o111)
        entrypoint_text = entrypoint.read_text()
        self.assertIn('case "${effective_uid}" in', entrypoint_text)
        self.assertIn("exec gosu 1000:1000", entrypoint_text)
        self.assertIn('[[ "${effective_gid}" != 1000 ]]', entrypoint_text)
        self.assertIn('exec "${entrypoint}" "$@"', entrypoint_text)
        self.assertIn(
            'entrypoint="/usr/local/bin/immich-docker-entrypoint.sh"',
            entrypoint_text,
        )

        server_manifest = self.artifacts[
            Path("usr/share/nas/storage/immich-server.storage-manifest")
        ]
        self.assertIn("tank/immich-server/library", server_manifest)
        self.assertIn("tank/immich-server/thumbs", server_manifest)
        self.assertIn("tank/immich-server/encoded-video", server_manifest)
        database_manifest = self.artifacts[
            Path("usr/share/nas/storage/immich-database.storage-manifest")
        ]
        self.assertIn("tank/immich-database/data", database_manifest)
        self.assertIn("|0700|", database_manifest)
        self.assertIn("recordsize=32K", database_manifest)

    def test_endpoint_consumers_generate_only_required_immich_edges(self):
        policy = self.artifacts[Path("etc/nftables/nas-krun-filter.nft")]
        expected = (
            (
                'iifname "krun-51130" oifname "krun-51140"',
                "tcp dport { 5432 } accept",
            ),
            (
                'iifname "krun-51130" oifname "krun-51150"',
                "tcp dport { 6379 } accept",
            ),
            (
                'iifname "krun-51130" oifname "krun-51160"',
                "tcp dport { 3003 } accept",
            ),
            (
                'iifname "krun-51310" oifname "krun-51130"',
                "tcp dport { 2283 } accept",
            ),
            (
                'iifname "krun-51230" oifname "krun-51130"',
                "tcp dport { 2283 } accept",
            ),
        )
        for source_and_destination, port in expected:
            with self.subTest(edge=source_and_destination):
                matching_line = next(
                    line
                    for line in policy.splitlines()
                    if source_and_destination in line
                )
                self.assertIn(port, matching_line)

    def test_ingress_monitoring_and_encrypted_secret_are_wired(self):
        caddyfile = (OVERLAY / "usr/share/nas/caddy/Caddyfile").read_text()
        self.assertIn("photos.i.samhclark.com", caddyfile)
        self.assertIn("reverse_proxy immich-server.krun:2283", caddyfile)

        scrape = (
            OVERLAY
            / "usr/share/nas/victoria-metrics/promscrape.yml"
        ).read_text()
        self.assertIn("job_name: 'immich-health'", scrape)
        self.assertIn("/api/server/ping", scrape)

        rules = (
            OVERLAY / "usr/share/nas/vmalert/alert-rules.yml"
        ).read_text()
        self.assertIn("alert: ImmichHealthDown", rules)
        self.assertIn("alert: ImmichHealthProbeBroken", rules)

        secrets = (
            REPO
            / "overlay-root/usr/share/nas/secrets/secrets.sops.yaml"
        ).read_text()
        self.assertIn("immich-db-password: ENC[AES256_GCM", secrets)


if __name__ == "__main__":
    unittest.main()
