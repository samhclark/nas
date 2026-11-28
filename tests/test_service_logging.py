# ABOUTME: Locks the first deployment group's application logging contracts.

import tomllib
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def load_service(name: str) -> dict:
    with (REPO / "quadlets" / f"{name}.toml").open("rb") as stream:
        return tomllib.load(stream)


class FirstDeploymentLoggingTests(unittest.TestCase):
    def test_victoria_components_emit_json_at_info_level(self):
        vmalert = load_service("vmalert")["container"]["exec"]
        alertmanager = load_service("alertmanager")["container"]["exec"]

        self.assertIn("-loggerFormat=json", vmalert)
        self.assertIn("-loggerLevel=INFO", vmalert)
        self.assertIn("--log.format=json", alertmanager)
        self.assertIn("--log.level=info", alertmanager)

    def test_blackbox_exporter_keeps_probe_logs_and_uses_json(self):
        blackbox = load_service("blackbox-exporter")["container"]["exec"]

        self.assertIn("--log.format=json", blackbox)
        # The default --log.prober=info is intentionally not overridden: probe
        # failures are useful operator evidence and remain visible.
        self.assertNotIn("--log.prober=error", blackbox)

    def test_grafana_uses_only_json_console_logging(self):
        environment = load_service("grafana")["container"]["environment"]

        self.assertEqual(environment["GF_LOG_MODE"], "console")
        self.assertEqual(environment["GF_LOG_CONSOLE_FORMAT"], "json")

    def test_plain_stderr_services_remain_in_scope(self):
        # Garage and the small Python exporter already write operator-relevant
        # diagnostics to stderr. Their images do not expose a portable JSON
        # switch, so collection should preserve those records unchanged.
        garage = load_service("garage")
        exporter = load_service("jellyfin-exporter")
        self.assertEqual(garage["container"]["log-driver"], "journald")
        self.assertEqual(exporter["container"]["log-driver"], "journald")
        # GARAGE_LOG_TO_JOURNALD speaks to a journald socket inside the
        # libkrun guest, not the host journal that receives Quadlet stderr.
        self.assertNotIn("GARAGE_LOG_TO_JOURNALD", garage["container"].get("environment", {}))


if __name__ == "__main__":
    unittest.main()
