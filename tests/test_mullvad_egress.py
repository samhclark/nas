# ABOUTME: Tests the fixed, fleet-level Mullvad egress contract.

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from quadletgen.compiler import compile_fleet
from quadletgen.model import ConfigError, Fleet
from quadletgen.parser import parse_fleet_egress, load_service
from quadletgen.secrets import verify_sops
from tests.quadlet_test_support import REPO, current_fleet, service_toml, parse_fleet_fixture


MULLVAD = """\
[egress.mullvad]
interface = "wg-arr"
address = "10.72.38.9/32"
peer-public-key = "xpZ3ZDEukbqKQvdHwaqKMUhsYhcYD3uLPUh1ACsVr1s="
endpoint = "23.144.160.86:3094"
allowed-ips = ["0.0.0.0/0"]
secret = "mullvad-private-key"
route-table = 51820
firewall-mark = 51820
"""


class MullvadEgressTests(unittest.TestCase):
    def write_fleet(self, directory: Path, source: str = MULLVAD) -> Path:
        path = directory / "_fleet.toml"
        path.write_text(source)
        return path

    def tap_service(
        self,
        *,
        name: str = "download",
        uid: int = 51999,
        subid_start: int = 519990000,
        ipv4: str = "10.253.99.2/30",
        egress: str = 'egress = "mullvad"',
        consumers: tuple[str, ...] = (),
    ):
        consumer_text = (
            "consumers = ["
            + ", ".join(f'"{consumer}"' for consumer in consumers)
            + "]"
            if consumers
            else ""
        )
        return service_toml(
            name=name,
            uid=uid,
            subid_start=subid_start,
            container=(
                '[[container.endpoints]]\n'
                'name = "http"\n'
                'port = 8080\n'
                f"{consumer_text}"
            ),
            krun=(
                "cpus = 1\nram-mib = 128\n"
                f'ipv4 = "{ipv4}"\n'
                'probe-endpoint = "http"\n'
                f"{egress}"
            ),
        )

    def test_authored_fleet_and_current_fleet_load_the_fixed_config(self):
        fleet = current_fleet()
        egress = fleet.egress

        self.assertIsNotNone(egress)
        assert egress is not None
        self.assertEqual(egress.interface, "wg-arr")
        self.assertEqual(str(egress.address), "10.72.38.9/32")
        self.assertEqual(
            egress.peer_public_key,
            "xpZ3ZDEukbqKQvdHwaqKMUhsYhcYD3uLPUh1ACsVr1s=",
        )
        self.assertEqual(str(egress.endpoint_address), "23.144.160.86")
        self.assertEqual(egress.endpoint_port, 3094)
        self.assertEqual(
            [str(item) for item in egress.allowed_ips],
            ["0.0.0.0/0"],
        )
        self.assertEqual(egress.secret_name, "mullvad-private-key")
        self.assertEqual(egress.route_table, 51820)
        self.assertEqual(egress.firewall_mark, 51820)

        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            path = self.write_fleet(Path(directory_name))
            parsed = parse_fleet_fixture(path, parse_fleet_egress)
        self.assertEqual(parsed, egress)

        verify_sops(
            fleet,
            REPO / "overlay-root/usr/share/nas/secrets/secrets.sops.yaml",
        )

    def test_renders_host_wireguard_and_networkd_dependency_contract(self):
        fleet = current_fleet()
        artifacts = {
            artifact.path: artifact.content for artifact in compile_fleet(fleet)
        }
        netdev = artifacts[Path("usr/lib/systemd/network/70-wg-arr.netdev")]
        network = artifacts[Path("usr/lib/systemd/network/70-wg-arr.network")]
        dropin = artifacts[
            Path(
                "etc/systemd/system/systemd-networkd.service.d/"
                "10-nas-krun-accounts.conf"
            )
        ]
        manager = artifacts[
            Path("etc/NetworkManager/conf.d/90-nas-krun-taps.conf")
        ]

        self.assertIn("Name=wg-arr\nKind=wireguard", netdev)
        self.assertIn(
            "PrivateKey=@network.wireguard.private.70-wg-arr",
            netdev,
        )
        self.assertNotIn("PrivateKeyFile=", netdev)
        self.assertIn("FirewallMark=51820", netdev)
        self.assertIn("RouteTable=51820", netdev)
        self.assertIn(
            "PublicKey=xpZ3ZDEukbqKQvdHwaqKMUhsYhcYD3uLPUh1ACsVr1s=",
            netdev,
        )
        self.assertIn("Endpoint=23.144.160.86:3094", netdev)
        self.assertIn("AllowedIPs=0.0.0.0/0", netdev)
        self.assertIn("Name=wg-arr\n\n[Link]\nRequiredForOnline=no", network)
        self.assertIn("Address=10.72.38.9/32", network)
        self.assertIn("LinkLocalAddressing=no", network)
        self.assertIn("IPv6AcceptRA=no", network)
        self.assertNotIn("DNS=", network)
        self.assertIn("Requires=sops-distribute-secrets.service", dropin)
        self.assertIn("sops-distribute-secrets.service", dropin.split("After=", 1)[1])
        self.assertIn(
            "LoadCredential=network.wireguard.private.70-wg-arr:"
            "/run/nas-secrets/mullvad/mullvad-private-key",
            dropin,
        )
        self.assertIn(
            "unmanaged-devices=interface-name:krun-*;interface-name:wg-arr",
            manager,
        )

    def test_renders_current_boot_readiness_boundary(self):
        artifacts = {
            artifact.path: artifact.content
            for artifact in compile_fleet(current_fleet())
        }
        script = artifacts[
            Path("usr/local/bin/nas-egress-mullvad-readiness.sh")
        ]
        unit = artifacts[
            Path("etc/systemd/system/nas-egress-mullvad.service")
        ]
        manifest = artifacts[
            Path("usr/share/nas/fleet/egress-units.list")
        ]

        self.assertIn("clear_readiness", script)
        self.assertIn(
            "# A marker from an earlier boot must never authorize a selected guest",
            script,
        )
        self.assertLess(
            script.index("clear_readiness\n    echo"),
            script.index("deadline=$((SECONDS + MAX_WAIT_SECONDS))"),
        )
        self.assertIn('ip -4 -o address show dev "${INTERFACE}"', script)
        self.assertIn('table "${ROUTE_TABLE}" default', script)
        self.assertIn('route get "${MAGICDNS}"', script)
        self.assertIn("nas_krun_forward", script)
        self.assertIn("nas_krun_nat", script)
        self.assertIn("/proc/sys/kernel/random/boot_id", script)
        self.assertIn("mv -f \"${READY_TEMP}\" \"${READY_FILE}\"", script)
        self.assertIn("not a WireGuard handshake", script)
        self.assertIn(
            'oifname \\\"${INTERFACE}\\\" accept',
            script,
        )
        self.assertIn(
            'oifname \\\"${INTERFACE}\\\" masquerade',
            script,
        )
        self.assertIn(
            "ExecStart=/usr/local/bin/nas-egress-mullvad-readiness.sh publish",
            unit,
        )
        self.assertIn(
            "ExecStop=/usr/local/bin/nas-egress-mullvad-readiness.sh clear",
            unit,
        )
        for dependency in (
            "sops-distribute-secrets.service",
            "systemd-networkd.service",
            "nftables.service",
            "tailscaled.service",
        ):
            self.assertIn(dependency, unit)
        self.assertIn("nas-egress-mullvad.service", manifest)

    def test_selected_tap_routing_rules_are_ordered_and_unselected_is_unchanged(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            selected_path = directory / "download.toml"
            peer_path = directory / "peer.toml"
            selected_path.write_text(self.tap_service())
            peer_path.write_text(
                self.tap_service(
                    name="peer",
                    uid=51998,
                    subid_start=519900000,
                    ipv4="10.253.98.2/30",
                    egress="",
                )
            )
            fleet = Fleet.build(
                [load_service(selected_path), load_service(peer_path)],
                egress=parse_fleet_fixture(self.write_fleet(directory), parse_fleet_egress),
            )
            artifacts = {
                artifact.path: artifact.content
                for artifact in compile_fleet(fleet)
            }

        selected_network = artifacts[
            Path("usr/lib/systemd/network/80-krun-51999.network")
        ]
        peer_network = artifacts[
            Path("usr/lib/systemd/network/80-krun-51998.network")
        ]
        expected = (
            ("10.0.0.0/8", 100),
            ("172.16.0.0/12", 101),
            ("192.168.0.0/16", 102),
            ("100.64.0.0/10", 103),
        )
        positions = []
        for destination, priority in expected:
            block = (
                "[RoutingPolicyRule]\n"
                "From=10.253.99.2/32\n"
                f"To={destination}\n"
                "Table=main\n"
                f"Priority={priority}"
            )
            self.assertIn(block, selected_network)
            positions.append(selected_network.index(block))
        vpn_block = (
            "[RoutingPolicyRule]\n"
            "From=10.253.99.2/32\n"
            "Table=51820\n"
            "Priority=200"
        )
        self.assertIn(vpn_block, selected_network)
        positions.append(selected_network.index(vpn_block))
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("[RoutingPolicyRule]", peer_network)

    def test_selected_filter_and_nat_are_fail_closed_after_peer_edges(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            selected_path = directory / "download.toml"
            peer_path = directory / "peer.toml"
            selected_path.write_text(self.tap_service())
            peer_path.write_text(
                self.tap_service(
                    name="peer",
                    uid=51998,
                    subid_start=519900000,
                    ipv4="10.253.98.2/30",
                    egress="",
                    consumers=("download",),
                )
            )
            fleet = Fleet.build(
                [load_service(selected_path), load_service(peer_path)],
                egress=parse_fleet_fixture(self.write_fleet(directory), parse_fleet_egress),
            )
            artifacts = {
                artifact.path: artifact.content
                for artifact in compile_fleet(fleet)
            }

        policy = artifacts[Path("etc/nftables/nas-krun-filter.nft")]
        nat = artifacts[Path("etc/nftables/nas-krun-nat.nft")]
        edge = (
            'iifname "krun-51999" oifname "krun-51998" '
            "ip saddr 10.253.99.2 ip daddr 10.253.98.2 "
            "tcp dport { 8080 } accept"
        )
        dns_tcp = (
            'iifname "krun-51999" ip saddr 10.253.99.2 '
            'ip daddr 100.100.100.100 oifname "tailscale0" '
            "tcp dport 53 accept"
        )
        dns_udp = dns_tcp.replace("tcp dport", "udp dport")
        vpn = (
            'iifname "krun-51999" ip saddr 10.253.99.2 '
            'ip daddr != 100.100.100.100 oifname "wg-arr" accept'
        )
        established_vpn = vpn.replace(
            " accept", " ct state established,related accept"
        )
        established_dns = (
            'iifname "krun-51999" ip saddr 10.253.99.2 '
            'ip daddr 100.100.100.100 oifname "tailscale0" '
            "tcp dport 53 ct state established,related accept"
        )
        established_peer = (
            'iifname "krun-51999" ip saddr 10.253.99.2 '
            'oifname "krun-51998" ct state established,related accept'
        )
        established_drop = (
            'iifname "krun-51999" ip saddr 10.253.99.2 '
            "ct state established,related drop"
        )
        fleet_established = "ct state established,related accept"
        final_drop = 'iifname "krun-51999" ip saddr 10.253.99.2 drop'
        for rule in (
            established_vpn,
            established_dns,
            established_peer,
            established_drop,
            edge,
            dns_tcp,
            dns_udp,
            vpn,
            final_drop,
        ):
            self.assertIn(rule, policy)
        self.assertLess(policy.index(established_vpn), policy.index(established_drop))
        self.assertLess(policy.index(established_dns), policy.index(established_drop))
        self.assertLess(policy.index(established_peer), policy.index(established_drop))
        fleet_established_position = policy.index(
            f"    {fleet_established}\n",
            policy.index(established_drop) + len(established_drop),
        )
        self.assertLess(policy.index(established_drop), fleet_established_position)
        self.assertLess(fleet_established_position, policy.index(edge))
        self.assertLess(policy.index(edge), policy.index(dns_tcp))
        self.assertLess(policy.index(dns_udp), policy.index(vpn))
        self.assertLess(policy.index(vpn), policy.index(final_drop))
        self.assertNotIn(
            'iifname "krun-51999" ip saddr 10.253.99.2 '
            'oifname != "krun-51998"',
            policy,
        )
        self.assertIn(
            'iifname "krun-51998" ip saddr 10.253.98.2 '
            'oifname != "krun-51998" oifname != "krun-51999" accept',
            policy,
        )
        self.assertIn(
            'ip saddr 10.253.99.2 ip daddr 100.100.100.100 '
            'oifname "tailscale0" masquerade',
            nat,
        )
        self.assertIn(
            'ip saddr 10.253.99.2 ip daddr != 100.100.100.100 '
            'oifname "wg-arr" masquerade',
            nat,
        )
        self.assertNotIn(
            'ip saddr 10.253.99.2 oifname != "krun-51998" masquerade',
            nat,
        )
        self.assertIn(
            'ip saddr 10.253.98.2 oifname != "krun-51998" '
            'oifname != "krun-51999" masquerade',
            nat,
        )

    def test_synthetic_selected_and_ordinary_policy_is_nft_valid_and_fail_closed(self):
        if shutil.which("nft") is None or shutil.which("unshare") is None:
            self.skipTest("nft and unshare are required for isolated policy validation")

        preflight = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", "true"],
            capture_output=True,
            text=True,
        )
        if preflight.returncode:
            self.skipTest(
                "unprivileged network namespaces unavailable: "
                + preflight.stderr.strip()
            )

        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            selected_path = directory / "selected.toml"
            ordinary_path = directory / "ordinary.toml"
            selected_path.write_text(
                self.tap_service(
                    name="selected",
                    uid=51999,
                    subid_start=519990000,
                    ipv4="10.253.99.2/30",
                )
            )
            ordinary_path.write_text(
                self.tap_service(
                    name="ordinary",
                    uid=51998,
                    subid_start=519900000,
                    ipv4="10.253.98.2/30",
                    egress="",
                )
            )
            fleet = Fleet.build(
                [load_service(selected_path), load_service(ordinary_path)],
                egress=parse_fleet_fixture(self.write_fleet(directory), parse_fleet_egress),
            )
            artifacts = {
                artifact.path: artifact.content
                for artifact in compile_fleet(fleet)
            }

        selected_network = artifacts[
            Path("usr/lib/systemd/network/80-krun-51999.network")
        ]
        ordinary_network = artifacts[
            Path("usr/lib/systemd/network/80-krun-51998.network")
        ]
        policy = artifacts[Path("etc/nftables/nas-krun-filter.nft")]
        nat = artifacts[Path("etc/nftables/nas-krun-nat.nft")]

        self.assertIn(
            "From=10.253.99.2/32\nTo=10.0.0.0/8\nTable=main\nPriority=100",
            selected_network,
        )
        self.assertIn(
            "From=10.253.99.2/32\nTable=51820\nPriority=200",
            selected_network,
        )
        self.assertNotIn("[RoutingPolicyRule]", ordinary_network)

        selected_vpn = (
            'iifname "krun-51999" ip saddr 10.253.99.2 '
            'ip daddr != 100.100.100.100 oifname "wg-arr" accept'
        )
        selected_drop = 'iifname "krun-51999" ip saddr 10.253.99.2 drop'
        established_drop = (
            'iifname "krun-51999" ip saddr 10.253.99.2 '
            "ct state established,related drop"
        )
        ordinary_accept = (
            'iifname "krun-51998" ip saddr 10.253.98.2 '
            'oifname != "krun-51998" oifname != "krun-51999" accept'
        )
        self.assertIn(selected_vpn, policy)
        self.assertIn(selected_drop, policy)
        self.assertIn(established_drop, policy)
        self.assertIn(ordinary_accept, policy)
        fleet_established_position = policy.index(
            "    ct state established,related accept\n",
            policy.index(established_drop) + len(established_drop),
        )
        self.assertLess(policy.index(established_drop), fleet_established_position)
        self.assertLess(policy.index(selected_vpn), policy.index(selected_drop))
        self.assertNotIn(
            'iifname "krun-51999" ip saddr 10.253.99.2 '
            'oifname != "krun-51998"',
            policy,
        )

        self.assertIn(
            'ip saddr 10.253.99.2 ip daddr != 100.100.100.100 '
            'oifname "wg-arr" masquerade',
            nat,
        )
        self.assertIn(
            'ip saddr 10.253.98.2 oifname != "krun-51998" '
            'oifname != "krun-51999" masquerade',
            nat,
        )
        self.assertNotIn(
            'ip saddr 10.253.99.2 oifname != "krun-51998" '
            'oifname != "krun-51999" masquerade',
            nat,
        )

        nft_input = "table inet test_filter {\n" + policy + "}\n" + nat
        result = subprocess.run(
            [
                "unshare",
                "--user",
                "--map-root-user",
                "--net",
                "nft",
                "-c",
                "-f",
                "-",
            ],
            input=nft_input,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_selected_policy_uses_typed_egress_interface(self):
        alternate = MULLVAD.replace('interface = "wg-arr"', 'interface = "wg-test"')
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            selected_path = directory / "download.toml"
            selected_path.write_text(self.tap_service())
            fleet = Fleet.build(
                [load_service(selected_path)],
                egress=parse_fleet_fixture(self.write_fleet(directory, alternate), parse_fleet_egress),
            )
            artifacts = {
                artifact.path: artifact.content
                for artifact in compile_fleet(fleet)
            }

        policy = artifacts[Path("etc/nftables/nas-krun-filter.nft")]
        nat = artifacts[Path("etc/nftables/nas-krun-nat.nft")]
        readiness = artifacts[
            Path("usr/local/bin/nas-egress-mullvad-readiness.sh")
        ]
        self.assertIn('oifname "wg-test" accept', policy)
        self.assertNotIn('oifname "wg-arr" accept', policy)
        self.assertIn('oifname "wg-test" masquerade', nat)
        self.assertNotIn('oifname "wg-arr" masquerade', nat)
        self.assertIn('INTERFACE=wg-test', readiness)

    def test_only_mullvad_tap_services_get_the_egress_wait(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            path = directory / "download.toml"
            path.write_text(self.tap_service())
            service = load_service(path)
            fleet = Fleet.build(
                [service],
                egress=parse_fleet_fixture(self.write_fleet(directory), parse_fleet_egress),
            )
            selected = compile_fleet(fleet)[0].content
            self.assertIn("/run/nas-egress/mullvad/ready 60 1", selected)

        current = current_fleet()
        current_artifacts = {
            artifact.path: artifact.content
            for artifact in compile_fleet(current)
        }
        for service in current.services:
            artifact = current_artifacts[
                Path(
                    "etc/containers/systemd/users/"
                    f"{service.host.uid}/{service.info.name}.container"
                )
            ]
            selected = getattr(service.krun, "egress", None) == "mullvad"
            assertion = self.assertIn if selected else self.assertNotIn
            assertion("/run/nas-egress/mullvad/ready", artifact)

    def test_egress_manifest_is_empty_without_fleet_egress(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            path = directory / "download.toml"
            path.write_text(self.tap_service(egress=""))
            service = load_service(path)
            artifacts = {
                artifact.path: artifact.content
                for artifact in compile_fleet(Fleet.build([service]))
            }
            manifest = artifacts[
                Path("usr/share/nas/fleet/egress-units.list")
            ]
        self.assertNotIn("nas-egress-mullvad.service", manifest)

    def test_manifest_and_distributor_preserve_root_secret_ownership_contract(self):
        fleet = current_fleet()
        artifacts = {
            artifact.path: artifact.content for artifact in compile_fleet(fleet)
        }
        manifest = artifacts[Path("usr/share/nas/fleet/secrets.tsv")]
        rows = [
            line.split("\t")
            for line in manifest.splitlines()
            if line and not line.startswith("#")
        ]
        self.assertIn(["mullvad", "root", "mullvad-private-key"], rows)

        distributor = (
            REPO / "overlay-root/usr/local/bin/sops-distribute-secrets.sh"
        ).read_text()
        self.assertIn('chown "${user}:${user}" "${tmp}"', distributor)
        self.assertIn('chmod 0400 "${tmp}"', distributor)
        self.assertIn("Root-owned consumers therefore receive root:root", distributor)
        self.assertNotIn("PrivateKey =", distributor)

        for artifact in artifacts.values():
            self.assertNotIn("PrivateKey =", artifact)

    def test_service_egress_requires_fleet_config(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            path = directory / "download.toml"
            path.write_text(self.tap_service())
            service = load_service(path)

        with self.assertRaisesRegex(ConfigError, "requires .*fleet config"):
            Fleet.build([service])

    def test_verify_sops_requires_the_fleet_egress_secret(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            service_path = directory / "download.toml"
            service_path.write_text(self.tap_service())
            service = load_service(service_path)
            fleet = Fleet.build(
                [service],
                egress=parse_fleet_fixture(self.write_fleet(directory), parse_fleet_egress),
            )
            sops_path = directory / "secrets.sops.yaml"
            sops_path.write_text("other-secret: ENC[test]\n")

            with self.assertRaisesRegex(
                ConfigError,
                "_fleet.toml: secret 'mullvad-private-key'",
            ):
                verify_sops(fleet, sops_path)

    def test_service_egress_requires_an_enabled_container(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            path = Path(directory_name) / "download.toml"
            path.write_text(
                service_toml(name="download", container="enabled = false")
                + 'egress = "mullvad"\n'
            )
            with self.assertRaisesRegex(ConfigError, "requires an active TAP"):
                load_service(path)

    def test_rejects_unknown_egress_value(self):
        with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
            directory = Path(directory_name)
            path = directory / "download.toml"
            path.write_text(self.tap_service(egress='egress = "other"'))
            with self.assertRaisesRegex(ConfigError, 'must be "mullvad"'):
                load_service(path)

    def test_rejects_invalid_mullvad_fields(self):
        cases = {
            "interface": 'interface = "interface-name-is-too-long"',
            "address": 'address = "10.72.38.9/31"',
            "peer key": 'peer-public-key = "not-base64"',
            "endpoint": 'endpoint = "[::1]:3094"',
            "allowed IPs": 'allowed-ips = ["10.0.0.0/8"]',
            "secret": 'secret = "bad secret"',
            "route table": "route-table = 0",
            "firewall mark": "firewall-mark = 0",
            "reserved route table": "route-table = 255",
            "mismatched mark": "firewall-mark = 51821",
            "non-WireGuard interface": 'interface = "tailscale0"',
        }
        for label, replacement in cases.items():
            with self.subTest(label=label):
                source = MULLVAD
                key = replacement.split(" = ", 1)[0]
                source = "\n".join(
                    replacement if line.startswith(key + " = ") else line
                    for line in source.splitlines()
                )
                with tempfile.TemporaryDirectory(dir=REPO) as directory_name:
                    path = self.write_fleet(Path(directory_name), source)
                    with self.assertRaises(ConfigError):
                        parse_fleet_fixture(path, parse_fleet_egress)


if __name__ == "__main__":
    unittest.main()
