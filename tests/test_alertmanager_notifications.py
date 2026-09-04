# ABOUTME: Keeps Pushover firing and recovery notifications distinguishable.

import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class AlertmanagerNotificationTests(unittest.TestCase):
    def test_pushover_title_identifies_firing_and_resolved_notifications(self):
        config = (
            REPO
            / "overlay-root/usr/share/nas/alertmanager/alertmanager.yml"
        ).read_text()

        self.assertIn("send_resolved: true", config)
        self.assertIn(
            "title: 'NAS {{ .Status }}: {{ .GroupLabels.alertname }}'",
            config,
        )
        self.assertNotIn("title: 'NAS Alert:", config)


if __name__ == "__main__":
    unittest.main()
