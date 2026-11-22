# ABOUTME: Verifies the first media-automation stack's identity, storage,
# private service edges, encrypted DNS, and fail-closed Mullvad egress.

from __future__ import annotations

import unittest
from pathlib import Path

from quadletgen.compiler import compile_fleet
from tests.quadlet_test_support import current_fleet


EXPECTED = {
    "sonarr": (51410, "10.253.14.2", 8989),
    "radarr": (51420, "10.253.15.2", 7878),
    "prowlarr": (51430, "10.253.16.2", 9696),
    "sabnzbd": (51440, "10.253.17.2", 8080),
    "bazarr": (51450, "10.253.20.2", 6767),
}


class ArrStackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fleet = current_fleet()
        cls.services = {
            service.info.name: service
            for service in cls.fleet.services
            if service.info.application == "media-automation"
        }
        cls.artifacts = {
            artifact.path: artifact.content
            for artifact in compile_fleet(cls.fleet)
        }

    def test_stack_has_pinned_mullvad_selected_guests(self):
        self.assertEqual(set(self.services), set(EXPECTED))
        for name, (_, address, port) in EXPECTED.items():
            with self.subTest(service=name):
                service = self.services[name]
                self.assertTrue(service.active_tap)
                self.assertEqual(service.tap_guest.ip.compressed, address)
                self.assertEqual(service.krun.egress, "mullvad")
                self.assertEqual(
                    tuple(map(str, service.container.dns)),
                    ("100.100.100.100",),
                )
                self.assertIn("@sha256:", service.container.image)
                self.assertEqual(service.endpoints_by_name["http"].port, port)
                self.assertIsNone(
                    service.endpoints_by_name["http"].publication
                )

    def test_generated_quadlets_wait_for_egress_and_map_identity(self):
        for name, (uid, _, _) in EXPECTED.items():
            with self.subTest(service=name):
                quadlet = self.artifacts[
                    Path(f"etc/containers/systemd/users/{uid}/{name}.container")
                ]
                self.assertIn(
                    "marker /run/nas-egress/mullvad/ready 60 1",
                    quadlet,
                )
                self.assertIn("User=1000:1000", quadlet)
                self.assertIn(f"UIDMap=+u1000:@{uid}:1", quadlet)
                expected_gid = 51430 if name == "prowlarr" else 52000
                self.assertIn(f"GIDMap=+g1000:@{expected_gid}:1", quadlet)
                self.assertNotIn("UserNS=", quadlet)
                self.assertNotIn("PublishPort=", quadlet)
                self.assertIn("DNS=100.100.100.100", quadlet)
                self.assertIn("HealthCmd=none", quadlet)
                status_lines = [
                    line for line in quadlet.splitlines() if line.startswith("SuccessExitStatus=")
                ]
                self.assertEqual(status_lines, ["SuccessExitStatus=156"] if name == "bazarr" else [])

    def test_dotnet_arr_quadlets_use_writable_guest_tmp(self):
        for name, uid in (
            ("sonarr", 51410),
            ("radarr", 51420),
            ("prowlarr", 51430),
        ):
            with self.subTest(service=name):
                quadlet = self.artifacts[
                    Path(f"etc/containers/systemd/users/{uid}/{name}.container")
                ]
                self.assertIn("Environment=TMPDIR=/tmp", quadlet)

    def test_sabnzbd_quadlet_propagates_exact_allowed_hostnames(self):
        quadlet = self.artifacts[
            Path("etc/containers/systemd/users/51440/sabnzbd.container")
        ]
        self.assertIn(
            "Environment=NAS_SABNZBD_ALLOWED_HOSTNAMES="
            "sabnzbd.i.samhclark.com,sabnzbd.krun",
            quadlet,
        )
        self.assertIn(
            "Environment=NAS_SABNZBD_COMPLETED_PERMISSIONS=2770",
            quadlet,
        )

    def test_shared_layout_is_writable_only_where_required(self):
        sonarr = self.artifacts[
            Path("etc/containers/systemd/users/51410/sonarr.container")
        ]
        radarr = self.artifacts[
            Path("etc/containers/systemd/users/51420/radarr.container")
        ]
        prowlarr = self.artifacts[
            Path("etc/containers/systemd/users/51430/prowlarr.container")
        ]
        sabnzbd = self.artifacts[
            Path("etc/containers/systemd/users/51440/sabnzbd.container")
        ]
        self.assertIn("Volume=/var/zfs/tank/videos/data:/data", sonarr)
        self.assertIn("Volume=/var/zfs/tank/videos/data:/data", radarr)
        self.assertNotIn("/var/zfs/tank/videos", prowlarr)
        self.assertIn(
            "Volume=/var/zfs/tank/videos/data/usenet:/data/usenet",
            sabnzbd,
        )
        bazarr = self.artifacts[
            Path("etc/containers/systemd/users/51450/bazarr.container")
        ]
        media_mounts = [
            line for line in bazarr.splitlines()
            if line.startswith("Volume=/var/zfs/tank/videos")
        ]
        self.assertEqual(
            media_mounts, ["Volume=/var/zfs/tank/videos/data/media:/data/media"]
        )
        self.assertIn("Volume=/var/lib/bazarr/config:/config", bazarr)
        for quadlet in (sonarr, radarr, sabnzbd, bazarr):
            self.assertIn("GroupAdd=keep-groups", quadlet)

        for name, (uid, _, _) in EXPECTED.items():
            if name == "prowlarr":
                continue
            with self.subTest(storage_manifest=name):
                manifest = self.artifacts[
                    Path(f"usr/share/nas/storage/{name}.storage-manifest")
                ]
                self.assertIn(
                    f"service|{name}|_nas_{name}|{uid}|{uid}|"
                    f"{uid}0000|65536|-|52000",
                    manifest,
                )

    def test_selected_policy_routes_dns_and_internet_without_fallback_nat(self):
        policy = self.artifacts[Path("etc/nftables/nas-krun-filter.nft")]
        nat = self.artifacts[Path("etc/nftables/nas-krun-nat.nft")]
        for name, (uid, address, _) in EXPECTED.items():
            with self.subTest(service=name):
                tap = f"krun-{uid}"
                self.assertIn(
                    f'iifname "{tap}" ip saddr {address} '
                    'ip daddr 100.100.100.100 oifname "tailscale0" '
                    "udp dport 53 accept",
                    policy,
                )
                self.assertIn(
                    f'iifname "{tap}" ip saddr {address} '
                    'ip daddr != 100.100.100.100 oifname "wg-arr" accept',
                    policy,
                )
                self.assertIn(
                    f'iifname "{tap}" ip saddr {address} drop',
                    policy,
                )
                self.assertIn(
                    f"ip saddr {address} ip daddr 100.100.100.100 "
                    'oifname "tailscale0" masquerade',
                    nat,
                )
                self.assertIn(
                    f"ip saddr {address} ip daddr != 100.100.100.100 "
                    'oifname "wg-arr" masquerade',
                    nat,
                )

                network = self.artifacts[
                    Path(f"usr/lib/systemd/network/80-{tap}.network")
                ]
                self.assertIn(f"From={address}/32\nTable=51820", network)
                self.assertIn("To=100.64.0.0/10\nTable=main", network)

    def test_only_declared_management_edges_are_open(self):
        policy = self.artifacts[Path("etc/nftables/nas-krun-filter.nft")]
        expected = (
            ("krun-51310", "krun-51410", 8989),
            ("krun-51310", "krun-51420", 7878),
            ("krun-51310", "krun-51430", 9696),
            ("krun-51310", "krun-51440", 8080),
            ("krun-51430", "krun-51410", 8989),
            ("krun-51430", "krun-51420", 7878),
            ("krun-51410", "krun-51430", 9696),
            ("krun-51420", "krun-51430", 9696),
            ("krun-51410", "krun-51440", 8080),
            ("krun-51420", "krun-51440", 8080),
            ("krun-51430", "krun-51440", 8080),
            ("krun-51310", "krun-51450", 6767),
            ("krun-51450", "krun-51410", 8989),
            ("krun-51450", "krun-51420", 7878),
        )
        for source, target, port in expected:
            with self.subTest(source=source, target=target, port=port):
                line = next(
                    line
                    for line in policy.splitlines()
                    if f'iifname "{source}" oifname "{target}"' in line
                )
                self.assertIn(f"tcp dport {{ {port} }} accept", line)

        self.assertEqual(
            set(self.services["bazarr"].endpoints_by_name["http"].consumers),
            {"caddy"},
        )
        self.assertEqual(
            {
                name
                for name, service in self.services.items()
                if "bazarr" in service.endpoints_by_name["http"].consumers
            },
            {"sonarr", "radarr"},
        )


if __name__ == "__main__":
    unittest.main()
