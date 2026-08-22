# ABOUTME: Verifies the configured-not-deployed media automation logging slice.

from __future__ import annotations

import stat
import tomllib
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


SERVICES = {
    "sonarr": (51410, "SONARR__LOG__CONSOLEFORMAT", "sonarr-entrypoint.sh"),
    "radarr": (51420, "RADARR__LOG__CONSOLEFORMAT", "radarr-entrypoint.sh"),
    "prowlarr": (51430, "PROWLARR__LOG__CONSOLEFORMAT", "prowlarr-entrypoint.sh"),
}


class MediaLoggingTests(unittest.TestCase):
    def test_servarr_services_use_journald_and_clef_console_json(self):
        for service, (uid, environment_name, adapter_name) in SERVICES.items():
            with self.subTest(service=service):
                config = self._load(service)
                container = config["container"]
                self.assertEqual(container["log-driver"], "journald")
                self.assertEqual(
                    container["environment"][environment_name], "Clef"
                )
                # The source declaration remains image-controlled and the
                # adapter still launches the binary directly, which emits its
                # console target while retaining the application's file logs.
                adapter = (
                    REPO
                    / "overlay-root"
                    / "usr/share/nas"
                    / service
                    / adapter_name
                )
                self.assertTrue(stat.S_IMODE(adapter.stat().st_mode) & 0o100)
                self.assertIn(f"/app/{service}/bin/", adapter.read_text())
                self.assertNotIn("disable-file-log", adapter.read_text())
                self.assertEqual(config["host"]["uid"], uid)

    def test_sabnzbd_forces_console_without_disabling_its_file_log(self):
        config = self._load("sabnzbd")
        container = config["container"]
        self.assertEqual(container["log-driver"], "journald")

        adapter = (
            REPO / "overlay-root/usr/share/nas/sabnzbd/sabnzbd-entrypoint.sh"
        )
        script = adapter.read_text()
        self.assertIn('--server "${family}" --console "$@"', script)
        self.assertNotIn("--disable-file-log", script)
        self.assertEqual(config["host"]["uid"], 51440)

    @staticmethod
    def _load(service: str) -> dict:
        with (REPO / "quadlets" / f"{service}.toml").open("rb") as stream:
            return tomllib.load(stream)


if __name__ == "__main__":
    unittest.main()
