# ABOUTME: Locks Jellyfin's host journald collection and local diagnostics.

import tomllib
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def load_jellyfin() -> dict:
    with (REPO / "quadlets/jellyfin.toml").open("rb") as stream:
        return tomllib.load(stream)


class JellyfinLoggingTests(unittest.TestCase):
    def test_jellyfin_container_stderr_uses_host_journald(self):
        container = load_jellyfin()["container"]

        self.assertEqual(container["log-driver"], "journald")

    def test_jellyfin_keeps_image_console_and_file_logging_defaults(self):
        service = load_jellyfin()
        container = service["container"]
        environment = container.get("environment", {})

        # Jellyfin 10.11.11's pinned image has a Console sink and a rolling
        # File sink in its bundled logging.json. Do not redirect or disable
        # either sink here; the host driver captures stdout/stderr while the
        # config export retains /config/log for app and ffmpeg diagnostics.
        self.assertNotIn("JELLYFIN_LOG_DIR", environment)
        self.assertNotIn("JELLYFIN_LOG_LEVEL", environment)
        self.assertNotIn("JELLYFIN_LOG_FILE", environment)

        exports = {
            export["container-path"]: export
            for storage in service["storage"]
            for export in storage.get("exports", [])
        }
        self.assertEqual(exports["/config"]["access"], "read-write")
        self.assertEqual(exports["/cache"]["access"], "read-write")


if __name__ == "__main__":
    unittest.main()
