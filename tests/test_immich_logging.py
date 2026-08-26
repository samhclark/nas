# ABOUTME: Locks the Immich application's host journald logging contract.

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def load_service(name: str) -> dict:
    with (REPO / "quadlets" / f"{name}.toml").open("rb") as stream:
        return tomllib.load(stream)


class ImmichLoggingTests(unittest.TestCase):
    def test_all_immich_services_select_host_journald(self):
        for name in (
            "immich-server",
            "immich-database",
            "immich-valkey",
            "immich-machine-learning",
        ):
            with self.subTest(service=name):
                self.assertEqual(load_service(name)["container"]["log-driver"], "journald")

    def test_server_uses_documented_json_logging(self):
        service = load_service("immich-server")["container"]
        self.assertEqual(service["environment"]["IMMICH_LOG_FORMAT"], "json")

    def test_machine_learning_uses_supported_plain_console_controls(self):
        service = load_service("immich-machine-learning")["container"]
        environment = service["environment"]
        self.assertEqual(environment["IMMICH_LOG_LEVEL"], "info")
        self.assertEqual(environment["NO_COLOR"], "true")
        self.assertNotIn("IMMICH_LOG_FORMAT", environment)

    def test_database_and_valkey_keep_foreground_logging_defaults(self):
        database = load_service("immich-database")["container"]
        valkey = load_service("immich-valkey")["container"]

        # Both images already write their useful operational diagnostics to
        # stderr/stdout with the declared foreground commands. Do not add
        # speculative file-logging or collector flags that could hide them
        # from the host journal.
        self.assertEqual(database["exec"], "postgres -c config_file=/etc/postgresql/postgresql.conf")
        self.assertEqual(
            valkey["entrypoint"],
            "/usr/share/nas/immich-valkey/immich-valkey-entrypoint.sh",
        )
        self.assertEqual(valkey["exec"], "--port 6379")
        for container in (database, valkey):
            self.assertNotIn("logging_collector", container.get("exec", ""))
            self.assertNotIn("logfile", container.get("exec", ""))


if __name__ == "__main__":
    unittest.main()
