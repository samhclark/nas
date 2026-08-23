# ABOUTME: Regression tests for the generated libkrun TAP network data plane.

import os
import shutil
import subprocess
import tempfile
import time
import tomllib
import unittest
import uuid
from pathlib import Path

from quadletgen.model import KrunNetwork
from tests.quadlet_test_support import current_fleet


REPO = Path(__file__).resolve().parents[1]
PATCH = (
    REPO / "patches/crun/0001-krun-add-tap-network-annotation.patch"
).read_text()
CONTAINERFILE = (REPO / "Containerfile").read_text()
FILTER = (
    REPO / "overlay-root/etc/nftables/nas-krun-filter.nft"
).read_text()
NAT = (REPO / "overlay-root/etc/nftables/nas-krun-nat.nft").read_text()
POLICY_SCRIPT = (
    REPO / "overlay-root/usr/local/bin/nas-krun-network-policy.sh"
).read_text()
POLICY_UNIT = (
    REPO / "overlay-root/etc/systemd/system/nas-krun-network-policy.service"
).read_text()
NFTABLES_DROPIN = (
    REPO
    / "overlay-root/etc/systemd/system/nftables.service.d/10-nas-krun-policy.conf"
).read_text()
NETWORKD_DROPIN = (
    REPO
    / "overlay-root/etc/systemd/system/systemd-networkd.service.d/10-nas-krun-accounts.conf"
).read_text()
SELINUX_POLICY = (
    REPO / "overlay-root/usr/share/selinux/targeted/nas-krun-tun.cil"
).read_text()


class KrunTapNetworkTests(unittest.TestCase):
    def load_configs(self):
        return list(current_fleet().services)

    def test_every_generated_microvm_uses_a_unique_tap_and_subnet(self):
        configs = self.load_configs()
        fleet = current_fleet()

        taps = set()
        subnets = set()
        for service in fleet.services:
            self.assertEqual(service.container.network, "host")
            self.assertIsNotNone(service.krun)
            assert service.krun is not None
            self.assertIs(service.krun.network, KrunNetwork.TAP)
            taps.add(service.tap_name)
            subnets.add(service.tap_guest.network)

        self.assertEqual(len(taps), len(configs))
        self.assertEqual(len(subnets), len(configs))

    def test_tap_quadlets_do_not_ask_podman_to_proxy_ports(self):
        for path in (REPO / "overlay-root/etc/containers/systemd/users").glob(
            "*/*.container"
        ):
            unit = path.read_text()
            self.assertIn("Annotation=krun.tap_name=", unit)
            self.assertIn("Network=host", unit)
            self.assertIn("AddDevice=/dev/net/tun", unit)
            self.assertNotIn("PublishPort=", unit)
            self.assertNotIn("Annotation=krun.use_passt", unit)
            self.assertIn("/run/nas-krun-network/policy-ready", unit)
            self.assertIn("ExecStartPost=/usr/bin/bash", unit)
            self.assertIn("/dev/tcp/", unit)

    def test_policy_failure_cannot_leave_tap_guests_running(self):
        self.assertIn(
            "BindsTo=nftables.service systemd-networkd.service", POLICY_UNIT
        )
        self.assertIn(
            "PartOf=nftables.service systemd-networkd.service", POLICY_UNIT
        )
        self.assertIn(
            "ExecStop=/usr/local/bin/nas-krun-network-policy.sh quiesce",
            POLICY_UNIT,
        )
        self.assertIn(
            'rm -f "${READY_FILE}" "${READY_FILE}.tmp"', POLICY_SCRIPT
        )
        self.assertIn(
            "trap 'clear_readiness; exit 1' HUP INT TERM", POLICY_SCRIPT
        )
        self.assertIn('systemctl stop "${USER_UNITS[@]}"', POLICY_SCRIPT)
        self.assertIn(
            'systemctl start --no-block "${USER_UNITS[@]}"', POLICY_SCRIPT
        )
        self.assertIn("nft list chain inet filter nas_krun_input", POLICY_SCRIPT)
        self.assertIn("systemd-networkd-wait-online", POLICY_SCRIPT)
        self.assertIn("ExecStop=\n", NFTABLES_DROPIN)
        self.assertIn("quiesce-and-flush", NFTABLES_DROPIN)
        self.assertLess(
            POLICY_SCRIPT.index('systemctl stop "${USER_UNITS[@]}"'),
            POLICY_SCRIPT.index("nft flush ruleset"),
        )

    def test_tap_device_selinux_access_is_narrow(self):
        self.assertEqual(
            SELINUX_POLICY,
            "(block nas_krun_tun\n"
            "  (allow container_kvm_t tun_tap_device_t "
            "(chr_file (open)))\n"
            "  (allow container_kvm_t systemd_networkd_t "
            "(tun_socket (relabelfrom)))\n"
            ")\n",
        )
        self.assertIn(
            "semodule --noreload --install \\\n"
            "        /usr/share/selinux/targeted/nas-krun-tun.cil",
            CONTAINERFILE,
        )
        self.assertNotIn("container_use_devices", CONTAINERFILE)
        self.assertFalse(
            (REPO / "scripts/validate-krun-tun-selinux.sh").exists(),
            "the retired live-policy mutation helper must not return",
        )

    def test_selinux_policy_changes_share_the_policy_store_copy_up_layer(self):
        policy_layer = CONTAINERFILE[
            CONTAINERFILE.index("RUN --mount=type=bind,from=zfs-rpms") :
            CONTAINERFILE.index("\n\nCOPY --from=crun-builder")
        ]
        policy_steps = (
            "cp -a /etc/selinux/targeted /etc/selinux/targeted.rebuilt",
            "rm -rf /etc/selinux/targeted",
            "mv /etc/selinux/targeted.rebuilt /etc/selinux/targeted",
            "dnf install -y",
            "/usr/share/selinux/targeted/nas-krun-tun.cil",
            "semanage fcontext -a",
        )
        positions = [policy_layer.index(step) for step in policy_steps]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(CONTAINERFILE.count("semodule --noreload --install"), 1)
        self.assertEqual(CONTAINERFILE.count("cp -a /etc/selinux/targeted"), 1)

    def test_generic_networkd_wait_online_is_disabled(self):
        self.assertIn(
            "systemctl disable systemd-networkd-wait-online.service",
            CONTAINERFILE,
        )
        self.assertIn(
            "/usr/lib/systemd/systemd-networkd-wait-online",
            POLICY_SCRIPT,
        )

    def test_networkd_grants_tap_to_service_and_enables_vnet_headers(self):
        config = (
            REPO / "overlay-root/usr/lib/systemd/network/80-krun-51120.netdev"
        ).read_text()
        network = (
            REPO / "overlay-root/usr/lib/systemd/network/80-krun-51120.network"
        ).read_text()

        self.assertIn("User=_nas_jellyfin", config)
        self.assertIn("Group=_nas_jellyfin", config)
        self.assertIn("VNetHeader=yes", config)
        self.assertNotIn("KeepCarrier=yes", config)
        self.assertIn("Address=10.253.2.1/30", network)
        self.assertIn("PoolOffset=2", network)
        self.assertIn("PoolSize=1", network)
        self.assertIn("PersistLeases=runtime", network)
        self.assertIn("RapidCommit=yes", network)
        self.assertIn("IPv4RouteLocalnet=yes", network)

    def test_networkd_waits_for_tap_owner_accounts(self):
        for service in self.load_configs():
            self.assertIn(
                f"ensure-nas-{service.host.slug}-account.service",
                NETWORKD_DROPIN,
            )

    def test_networkd_automatic_restart_rearms_policy(self):
        self.assertIn("Wants=nas-krun-network-policy.service", NETWORKD_DROPIN)

        if not shutil.which("systemctl") or not os.environ.get("XDG_RUNTIME_DIR"):
            self.skipTest("a running systemd user manager is unavailable")
        probe = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
        )
        if probe.stdout.strip() not in {"running", "degraded"}:
            self.skipTest("a running systemd user manager is unavailable")

        suffix = uuid.uuid4().hex
        dependency = f"test-krun-networkd-{suffix}.service"
        policy = f"test-krun-policy-{suffix}.service"
        unit_dir = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user"
        unit_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="krun-policy-test-") as state_dir:
            state = Path(state_dir)
            attempts = state / "attempts"
            events = state / "events"
            dependency_path = unit_dir / dependency
            policy_path = unit_dir / policy
            dependency_path.write_text(
                "\n".join(
                    [
                        "[Unit]",
                        f"Wants={policy}",
                        "",
                        "[Service]",
                        "Type=simple",
                        "ExecStart=/bin/bash -ceu '"
                        f"count=$(cat {attempts} 2>/dev/null || echo 0); "
                        f"count=$((count + 1)); echo $count > {attempts}; "
                        'if (( count == 1 )); then sleep 0.5; exit 1; fi; '
                        "exec sleep 30'",
                        "Restart=on-failure",
                        "RestartSec=0.1",
                        "",
                    ]
                )
            )
            policy_path.write_text(
                "\n".join(
                    [
                        "[Unit]",
                        f"BindsTo={dependency}",
                        f"After={dependency}",
                        "",
                        "[Service]",
                        "Type=oneshot",
                        f"ExecStart=/bin/bash -c 'echo start >> {events}'",
                        f"ExecStop=/bin/bash -c 'echo stop >> {events}'",
                        "RemainAfterExit=yes",
                        "",
                    ]
                )
            )

            try:
                subprocess.run(
                    ["systemctl", "--user", "daemon-reload"], check=True
                )
                subprocess.run(
                    ["systemctl", "--user", "start", dependency], check=True
                )

                deadline = time.monotonic() + 10
                observed = []
                while time.monotonic() < deadline:
                    observed = (
                        events.read_text().splitlines() if events.exists() else []
                    )
                    if observed[:3] == ["start", "stop", "start"]:
                        break
                    time.sleep(0.1)

                restart_count = subprocess.run(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        dependency,
                        "--property=NRestarts",
                        "--value",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                self.assertGreaterEqual(int(restart_count), 1)
                self.assertEqual(observed[:3], ["start", "stop", "start"])
                self.assertEqual(
                    subprocess.run(
                        ["systemctl", "--user", "is-active", policy],
                        capture_output=True,
                        text=True,
                    ).stdout.strip(),
                    "active",
                )
            finally:
                subprocess.run(
                    ["systemctl", "--user", "stop", dependency, policy],
                    capture_output=True,
                )
                dependency_path.unlink(missing_ok=True)
                policy_path.unlink(missing_ok=True)
                subprocess.run(
                    ["systemctl", "--user", "daemon-reload"],
                    capture_output=True,
                )

    def test_nft_policy_has_antispoof_edges_publication_and_nat(self):
        self.assertIn(
            'iifname "krun-51120" ip saddr != 10.253.2.2 drop', FILTER
        )
        self.assertIn(
            'iifname "krun-51310" oifname "krun-51120" '
            "ip saddr 10.253.9.2 ip daddr 10.253.2.2 tcp dport { 8096 } accept",
            FILTER,
        )
        self.assertIn("tcp dport 443 dnat to 10.253.9.2:443", NAT)
        self.assertIn(
            "ip daddr 127.0.0.1 tcp dport 8096 dnat to 10.253.2.2:8096",
            NAT,
        )
        self.assertIn("ip saddr 10.253.2.2", NAT)
        self.assertIn("masquerade", NAT)
        self.assertIn(
            "ip saddr 10.253.7.2 ip daddr 10.253.7.1 tcp dport 9100 accept",
            FILTER,
        )
        self.assertIn(
            'oifname "krun-51120" ip saddr 127.0.0.0/8 '
            "ip daddr 10.253.2.2 snat to 10.253.2.1",
            NAT,
        )
        self.assertIn(
            'iifname "krun-51230" oifname "krun-51110" '
            "ip saddr 10.253.5.2 ip daddr 10.253.1.2 tcp dport { 3903 } accept",
            FILTER,
        )

    def test_loopback_dnat_has_a_real_return_path(self):
        required = ("unshare", "ip", "nsenter", "nft", "curl", "python3")
        missing = [command for command in required if shutil.which(command) is None]
        if missing:
            self.skipTest(f"network namespace tools unavailable: {', '.join(missing)}")

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

        script = r'''
ip link set lo up
unshare --net sleep 30 &
guest_pid=$!
server_pid=
cleanup() {
    [[ -n "$server_pid" ]] && kill "$server_pid" 2>/dev/null || true
    kill "$guest_pid" 2>/dev/null || true
}
trap cleanup EXIT

ip link add krun-test type veth peer name eth0
ip link set eth0 netns "$guest_pid"
ip addr add 10.254.254.1/30 dev krun-test
ip link set krun-test up
nsenter -t "$guest_pid" -n ip link set lo up
nsenter -t "$guest_pid" -n ip addr add 10.254.254.2/30 dev eth0
nsenter -t "$guest_pid" -n ip link set eth0 up
nsenter -t "$guest_pid" -n ip route add default via 10.254.254.1
sysctl -q -w net.ipv4.conf.krun-test.route_localnet=1

nft -f - <<'EOF'
table ip test_nat {
    chain output {
        type nat hook output priority dstnat; policy accept;
        ip daddr 127.0.0.1 tcp dport 18096 dnat to 10.254.254.2:8096
    }
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        oifname "krun-test" ip saddr 127.0.0.0/8 ip daddr 10.254.254.2 snat to 10.254.254.1
    }
}
EOF

nsenter -t "$guest_pid" -n python3 -m http.server 8096 --bind 10.254.254.2 \
    >/dev/null 2>&1 &
server_pid=$!
for _ in 1 2 3 4 5; do
    curl -fsS --max-time 2 http://127.0.0.1:18096/ >/dev/null && exit 0
    sleep 0.2
done
exit 1
'''
        result = subprocess.run(
            [
                "unshare",
                "--user",
                "--map-root-user",
                "--net",
                "--mount",
                "--fork",
                "bash",
                "-ceu",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_crun_patch_calls_libkrun_tap_with_dhcp(self):
        self.assertIn('find_annotation (container, "krun.tap_name")', PATCH)
        self.assertIn('dlsym (handle, "krun_add_net_tap")', PATCH)
        self.assertIn("COMPAT_NET_FEATURES, NET_FLAG_DHCP_CLIENT", PATCH)
        self.assertIn("krun.tap_name and krun.use_passt are mutually exclusive", PATCH)

    def test_crun_source_is_verified_before_extraction(self):
        archives = (
            (
                "https://github.com/containers/crun/archive/refs/tags/1.29.1.tar.gz",
                "ac6017a905eb21ba76389ed7327e8f7d6ca55a7c20c69e6c71fb450d4e358c77",
                "crun-1.29.1.tar.gz",
            ),
            (
                "https://github.com/containers/libocispec/archive/872b8b0b7ccb1a121601ede0dcac8c6b8a1008a6.tar.gz",
                "9cb0bcd43e25784b44f113045251cf09c291b912c0b862bd505be1a9027c0825",
                "libocispec-872b8b0b7ccb1a121601ede0dcac8c6b8a1008a6.tar.gz",
            ),
            (
                "https://github.com/opencontainers/runtime-spec/archive/d64c1d945da7cf6970061c7c9ff4391fafdf2a15.tar.gz",
                "1698ebaa7ff07f8409c084fe9539d0391820e71b7a6e6d877aa1ce8b383a4b50",
                "runtime-spec-d64c1d945da7cf6970061c7c9ff4391fafdf2a15.tar.gz",
            ),
            (
                "https://github.com/opencontainers/image-spec/archive/26647a49f642c7d22a1cd3aa0a48e4650a542269.tar.gz",
                "8668357de6a1162220b2d1fb654a4182a55844b90ad2774c3b99640eec7e2f54",
                "image-spec-26647a49f642c7d22a1cd3aa0a48e4650a542269.tar.gz",
            ),
        )
        for url, checksum, filename in archives:
            self.assertIn(url, CONTAINERFILE)
            self.assertIn(f"{checksum}  /tmp/{filename}", CONTAINERFILE)

        self.assertIn("tar --extract --gzip --file /tmp/crun-1.29.1.tar.gz", CONTAINERFILE)
        self.assertIn("sha256sum --check --strict", CONTAINERFILE)
        self.assertNotIn("ADD --checksum", CONTAINERFILE)
        self.assertLess(
            CONTAINERFILE.index("sha256sum --check --strict"),
            CONTAINERFILE.index("tar --extract --gzip"),
        )


if __name__ == "__main__":
    unittest.main()
