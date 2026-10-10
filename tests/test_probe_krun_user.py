# ABOUTME: Unit-tests krun identity probe command construction and classification.

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/probe-krun-user.py"
SPEC = importlib.util.spec_from_file_location("probe_krun_user", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PROBE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROBE
SPEC.loader.exec_module(PROBE)


class KrunUserProbeTests(unittest.TestCase):
    def test_command_uses_pinned_image_and_krun_identity_contract(self):
        command = PROBE.probe_command("example/image@sha256:abc", "fake-podman")

        self.assertEqual(command[:2], ["fake-podman", "run"])
        self.assertIn("--runtime=krun", command)
        self.assertIn("--user=1000:1000", command)
        self.assertIn("--userns=keep-id:uid=1000,gid=1000", command)
        self.assertIn("--entrypoint=/usr/bin/id", command)
        self.assertEqual(command[-1], "example/image@sha256:abc")

    def test_classifies_guest_root_fallback(self):
        self.assertEqual(
            PROBE.classify_identity("uid=0(root) gid=0(root) groups=0(root)\n"),
            "guest-root-fallback",
        )

    def test_valkey_bootstrap_requests_guest_root_without_changing_host_user(self):
        command = PROBE.probe_command(
            "example/image@sha256:abc", "fake-podman", guest_bootstrap_root=True,
        )
        self.assertIn("--annotation=krun.guest_bootstrap_root=1", command)
        self.assertIn("--user=1000:1000", command)
        self.assertIn("--userns=keep-id:uid=1000,gid=1000", command)
        self.assertNotIn("--user=0:0", command)

    def test_classifies_honored_identity(self):
        self.assertEqual(
            PROBE.classify_identity("uid=1000 gid=1000 groups=1000\n"),
            "honored-1000:1000",
        )

    def test_rejects_unexpected_identity(self):
        with self.assertRaisesRegex(ValueError, "unexpected krun identity"):
            PROBE.classify_identity("uid=1001 gid=1001 groups=1001\n")


if __name__ == "__main__":
    unittest.main()
