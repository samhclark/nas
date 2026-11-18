# ABOUTME: Regression tests for Jellyfin's Caddy route and blackbox-based
# availability monitoring.

import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CADDYFILE = (
    REPO / "overlay-root/usr/share/nas/caddy/Caddyfile"
).read_text()
SCRAPE_CONFIG = (
    REPO
    / "overlay-root/usr/share/nas/victoria-metrics/promscrape.yml"
).read_text()
ALERT_RULES = (
    REPO / "overlay-root/usr/share/nas/vmalert/alert-rules.yml"
).read_text()
DASHBOARD = (
    REPO / "overlay-root/usr/share/nas/grafana/dashboards/jellyfin.json"
).read_text()
JELLYFIN_QUADLET = (
    REPO / "overlay-root/etc/containers/systemd/users/51120/jellyfin.container"
).read_text()
LIVE_DIAGNOSTIC = (REPO / "scripts/diagnostics/jellyfin-live.sh").read_text()


class JellyfinIntegrationTests(unittest.TestCase):
    def test_caddy_routes_selected_hostname_to_jellyfin_tap(self):
        self.assertIn(
            "jellyfin.i.samhclark.com {\n"
            "\treverse_proxy jellyfin.krun:8096\n"
            "}",
            CADDYFILE,
        )

    def test_jellyfin_uses_tap_and_disables_unsupported_exec_healthcheck(self):
        self.assertIn("Network=host", JELLYFIN_QUADLET)
        self.assertIn("Annotation=krun.tap_name=krun-51120", JELLYFIN_QUADLET)
        self.assertIn("HealthCmd=none", JELLYFIN_QUADLET)
        self.assertNotIn("PublishPort=", JELLYFIN_QUADLET)

    def test_victoria_metrics_probes_jellyfin_health_via_blackbox(self):
        self.assertIn("job_name: 'jellyfin-health'", SCRAPE_CONFIG)
        self.assertIn(
            "targets: ['http://jellyfin.krun:8096/health']",
            SCRAPE_CONFIG,
        )
        self.assertIn("replacement: blackbox-exporter.krun:9115", SCRAPE_CONFIG)

    def test_live_diagnostic_uses_the_tap_address(self):
        self.assertIn(
            "target=http://jellyfin.krun:8096/health", LIVE_DIAGNOSTIC
        )
        self.assertNotIn(
            "target=http://127.0.0.1:8096/health", LIVE_DIAGNOSTIC
        )

    def test_alerts_distinguish_service_failure_from_probe_failure(self):
        self.assertIn("alert: JellyfinHealthDown", ALERT_RULES)
        self.assertIn(
            'probe_success{job="jellyfin-health",service="jellyfin"} == 0',
            ALERT_RULES,
        )
        self.assertIn("alert: JellyfinHealthProbeBroken", ALERT_RULES)
        self.assertIn('up{job="jellyfin-health"} == 0', ALERT_RULES)

    def test_victoria_metrics_scrapes_playback_exporter(self):
        self.assertIn("job_name: 'jellyfin-exporter'", SCRAPE_CONFIG)
        self.assertIn("targets: ['jellyfin-exporter.krun:9594']", SCRAPE_CONFIG)

    def test_playback_dashboard_contains_live_and_stall_diagnostics(self):
        self.assertIn('"uid": "jellyfin-playback"', DASHBOARD)
        self.assertIn("jellyfin_playback_info", DASHBOARD)
        self.assertIn("jellyfin_transcode_speed_ratio", DASHBOARD)
        self.assertIn("jellyfin_playback_position_seconds", DASHBOARD)
        self.assertIn('{{username}} · {{title}} · {{device}}', DASHBOARD)
        self.assertIn('"username": 0', DASHBOARD)
        self.assertIn('"username": "User"', DASHBOARD)
        self.assertNotIn('"remote_endpoint":', DASHBOARD)


if __name__ == "__main__":
    unittest.main()
