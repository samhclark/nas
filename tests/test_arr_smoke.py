# ABOUTME: Unit-tests command construction for the opt-in *arr image smoke.

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/smoke-arr-images.py"
SPEC = importlib.util.spec_from_file_location("smoke_arr_images", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
SMOKE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SMOKE
SPEC.loader.exec_module(SMOKE)


class ArrSmokeUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fleet = SMOKE.load_fleet(SMOKE.FLEET_PATH.parent)

    def test_authored_services_and_assets_are_loaded(self):
        for name in SMOKE.SERVICE_NAMES:
            with self.subTest(service=name):
                spec = self.fleet.services_by_name[name]
                self.assertRegex(spec.container.image, r"@sha256:[0-9a-f]{64}$")
                self.assertIsNotNone(spec.assets)
                self.assertIsNotNone(spec.container.entrypoint)

    def test_runtime_modes_construct_distinct_podman_commands(self):
        self.assertEqual(
            SMOKE.runtime_probe_arguments(SMOKE.PODMAN_MODE),
            ["info", "--format={{.Host.OCIRuntime.Name}}"],
        )
        self.assertEqual(
            SMOKE.runtime_probe_arguments(SMOKE.KRUN_MODE),
            ["--runtime=krun", "info", "--format={{.Host.OCIRuntime.Name}}"],
        )

    def test_container_arguments_use_authored_adapter_and_disposable_mounts(self):
        with tempfile.TemporaryDirectory(dir="/var/tmp") as root:
            temporary = Path(root)
            resources = self.fleet.resources_by_name
            spec = self.fleet.services_by_name["sonarr"]
            arguments = SMOKE.container_arguments(
                spec,
                SMOKE.PODMAN_MODE,
                "arr-smoke-sonarr-test",
                temporary,
                resources,
            )

            self.assertEqual(arguments[0], "run")
            self.assertIn("--network=none", arguments)
            self.assertIn("--user=1000:1000", arguments)
            self.assertIn("--userns=keep-id:uid=1000,gid=1000", arguments)
            self.assertIn("--group-add=keep-groups", arguments)
            self.assertIn("--env", arguments)
            self.assertIn("TZ=America/Chicago", arguments)
            self.assertIn(
                "--entrypoint=/usr/share/nas/sonarr/sonarr-entrypoint.sh",
                arguments,
            )
            volume_values = [
                arguments[index + 1]
                for index, value in enumerate(arguments[:-1])
                if value == "--volume"
            ]
            self.assertTrue(any(":/usr/share/nas/sonarr:ro,Z" in value for value in volume_values))
            self.assertTrue(any(":/config:rw,Z" in value for value in volume_values))
            self.assertTrue(any(":/data:rw,Z" in value for value in volume_values))
            self.assertTrue(all(str(temporary) in value for value in volume_values))

    def test_each_authored_service_mounts_its_image_controlled_adapter(self):
        with tempfile.TemporaryDirectory(dir="/var/tmp") as root:
            temporary = Path(root)
            resources = self.fleet.resources_by_name
            for service_name in SMOKE.SERVICE_NAMES:
                with self.subTest(service=service_name):
                    spec = self.fleet.services_by_name[service_name]
                    arguments = SMOKE.container_arguments(
                        spec,
                        SMOKE.PODMAN_MODE,
                        f"arr-smoke-{service_name}-test",
                        temporary,
                        resources,
                    )
                    volume_values = [
                        arguments[index + 1]
                        for index, value in enumerate(arguments[:-1])
                        if value == "--volume"
                    ]
                    assert spec.assets is not None
                    self.assertTrue(
                        any(
                            f":{spec.assets.path}:ro,Z" in value
                            for value in volume_values
                        )
                    )
                    self.assertTrue(
                        all(str(temporary) in value for value in volume_values)
                    )

    def test_krun_command_selects_runtime_without_exec(self):
        with tempfile.TemporaryDirectory(dir="/var/tmp") as root:
            temporary = Path(root)
            resources = self.fleet.resources_by_name
            arguments = SMOKE.container_arguments(
                self.fleet.services_by_name["sabnzbd"],
                SMOKE.KRUN_MODE,
                "arr-smoke-sabnzbd-test",
                temporary,
                resources,
            )
            self.assertEqual(arguments[0], "--runtime=krun")
            self.assertEqual(arguments[1], "run")
            self.assertIn("--user=1000:1000", arguments)
            self.assertNotIn("exec", arguments)
            self.assertIn("--entrypoint=/usr/share/nas/sabnzbd/sabnzbd-entrypoint.sh", arguments)

    def test_both_runtime_modes_request_each_service_mapped_identity(self):
        with tempfile.TemporaryDirectory(dir="/var/tmp") as root:
            temporary = Path(root)
            resources = self.fleet.resources_by_name
            for mode in (SMOKE.PODMAN_MODE, SMOKE.KRUN_MODE):
                for service_name in SMOKE.SERVICE_NAMES:
                    with self.subTest(mode=mode.name, service=service_name):
                        spec = self.fleet.services_by_name[service_name]
                        mapped_id = spec.identity.mapped_container_id
                        self.assertIsNotNone(mapped_id)
                        arguments = SMOKE.container_arguments(
                            spec,
                            mode,
                            f"arr-smoke-{service_name}-{mode.name}-test",
                            temporary,
                            resources,
                        )
                        self.assertIn(
                            f"--user={mapped_id}:{mapped_id}",
                            arguments,
                        )

    def test_cleanup_names_are_narrow_and_forceful(self):
        name = SMOKE.container_name("radarr", SMOKE.KRUN_MODE)
        self.assertRegex(name, r"^arr-smoke-radarr-krun-[0-9]+-[0-9a-f]{12}$")
        self.assertEqual(SMOKE.cleanup_name(name), ["rm", "--force", "--ignore", name])

    def test_cleanup_failure_is_fatal(self):
        failed = mock.Mock(returncode=125, stdout="", stderr="still running")
        with mock.patch.object(SMOKE, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "cleanup .* failed"):
                SMOKE.cleanup_container("arr-smoke-sonarr-podman-1-deadbeefdead")

    def test_cleanup_verifies_the_container_is_gone(self):
        removed = mock.Mock(returncode=0, stdout="", stderr="")
        present = mock.Mock(returncode=0, stdout="true running", stderr="")
        with mock.patch.object(SMOKE, "run", side_effect=(removed, present)):
            with self.assertRaisesRegex(RuntimeError, "still exists"):
                SMOKE.cleanup_container("arr-smoke-sonarr-podman-1-deadbeefdead")

    def test_cleanup_failure_does_not_mask_the_startup_failure(self):
        startup = mock.Mock(returncode=125, stdout="", stderr="launch failed")
        state = mock.Mock(returncode=0, stdout='{"Status":"exited"}', stderr="")
        logs = mock.Mock(returncode=0, stdout="application failed", stderr="")
        cleanup = mock.Mock(returncode=125, stdout="", stderr="still running")
        with tempfile.TemporaryDirectory(dir="/var/tmp") as root:
            resources = self.fleet.resources_by_name
            with (
                mock.patch.object(SMOKE, "container_arguments", return_value=["run"]),
                mock.patch.object(
                    SMOKE,
                    "run",
                    side_effect=(startup, state, logs, cleanup),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "podman run failed") as raised:
                    SMOKE.smoke_service(
                        self.fleet.services_by_name["sonarr"],
                        SMOKE.PODMAN_MODE,
                        Path(root),
                        resources,
                        startup_timeout_seconds=1,
                        observation_seconds=1,
                    )
        self.assertTrue(
            any("cleanup of" in note for note in raised.exception.__notes__)
        )

    def test_runtime_probe_can_skip_unavailable_krun_without_launching(self):
        result = mock.Mock(returncode=125, stdout="", stderr="runtime missing")
        with mock.patch.object(SMOKE, "run", return_value=result) as mocked:
            self.assertFalse(SMOKE.runtime_available(SMOKE.KRUN_MODE))
        mocked.assert_called_once_with(
            ["--runtime=krun", "info", "--format={{.Host.OCIRuntime.Name}}"],
            capture=True,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
