# ABOUTME: Behaviorally tests the generic ZFS snapshot retention helper.

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "overlay-root/usr/local/bin/zfs-snapshot-retained.sh"
SYSTEMD = REPO / "overlay-root/etc/systemd/system"


FAKE_ZFS = """#!/bin/bash
set -euo pipefail

command="$1"
shift
case "${command}" in
    list)
        if [[ " $* " == *" -t snapshot "* ]]; then
            [[ "${FAKE_ZFS_FAIL_LISTING:-0}" == 0 ]] || exit 1
            cat "${FAKE_ZFS_STATE}"
        else
            target="${*: -1}"
            if [[ "${target}" == *@* ]]; then
                awk -F '\t' -v target="${target}" \
                    '$1 == target { print $1; found = 1 } END { exit !found }' \
                    "${FAKE_ZFS_STATE}"
            elif [[ "${FAKE_ZFS_DATASET_EXISTS:-1}" == 1 ]]; then
                printf '%s\n' "${target}"
            else
                exit 1
            fi
        fi
        ;;
    snapshot)
        printf 'snapshot\t%s\n' "$1" >> "${FAKE_ZFS_LOG}"
        [[ "${FAKE_ZFS_FAIL_SNAPSHOT:-0}" == 0 ]] || exit 1
        if awk -F '\t' -v target="$1" '$1 == target { found = 1 } END { exit !found }' \
            "${FAKE_ZFS_STATE}"; then
            exit 1
        fi
        printf '%s\t999\n' "$1" >> "${FAKE_ZFS_STATE}"
        ;;
    destroy)
        printf 'destroy\t%s\n' "$1" >> "${FAKE_ZFS_LOG}"
        [[ "${FAKE_ZFS_FAIL_DESTROY:-0}" == 0 ]] || exit 1
        awk -F '\t' -v target="$1" '$1 != target' \
            "${FAKE_ZFS_STATE}" > "${FAKE_ZFS_STATE}.new"
        mv "${FAKE_ZFS_STATE}.new" "${FAKE_ZFS_STATE}"
        ;;
    *)
        echo "unexpected zfs command: ${command}" >&2
        exit 2
        ;;
esac
"""


FAKE_DATE = """#!/bin/bash
set -euo pipefail
printf '%s\n' "${FAKE_TIMESTAMP}"
"""


class SnapshotRetentionTests(unittest.TestCase):
    def test_videos_retention_stops_after_the_one_month_window(self):
        containerfile = (REPO / "Containerfile").read_text()

        for cadence in ("frequently", "hourly", "daily", "weekly"):
            self.assertIn(
                f"zfs-snapshots-{cadence}@videos.timer",
                containerfile,
            )
        for cadence in ("monthly", "yearly"):
            self.assertNotIn(
                f"zfs-snapshots-{cadence}@videos.timer",
                containerfile,
            )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=REPO)
        self.directory = Path(self.temporary.name)
        self.state = self.directory / "state.tsv"
        self.log = self.directory / "commands.tsv"
        self.zfs = self.directory / "zfs"
        self.date = self.directory / "date"
        self.state.write_text("")
        self.log.write_text("")
        self.zfs.write_text(FAKE_ZFS)
        self.date.write_text(FAKE_DATE)
        self.zfs.chmod(0o755)
        self.date.chmod(0o755)
        self.environment = os.environ | {
            "ZFS_BIN": str(self.zfs),
            "DATE_BIN": str(self.date),
            "FAKE_ZFS_STATE": str(self.state),
            "FAKE_ZFS_LOG": str(self.log),
            "FAKE_TIMESTAMP": "20260808T120000Z",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def run_retention(
        self,
        cadence: str,
        retention: int,
        *,
        fail_snapshot: bool = False,
        fail_listing: bool = False,
        fail_destroy: bool = False,
        dataset_exists: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment.copy()
        if fail_snapshot:
            environment["FAKE_ZFS_FAIL_SNAPSHOT"] = "1"
        if fail_listing:
            environment["FAKE_ZFS_FAIL_LISTING"] = "1"
        if fail_destroy:
            environment["FAKE_ZFS_FAIL_DESTROY"] = "1"
        if not dataset_exists:
            environment["FAKE_ZFS_DATASET_EXISTS"] = "0"
        return subprocess.run(
            [SCRIPT, "tank/videos", cadence, str(retention)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=3,
        )

    def seed(self, snapshots: list[tuple[str, int]]) -> None:
        self.state.write_text(
            "".join(
                f"tank/videos@{name}\t{creation}\n"
                for name, creation in snapshots
            )
        )

    def commands(self) -> list[tuple[str, str]]:
        return [
            tuple(line.split("\t", 1))
            for line in self.log.read_text().splitlines()
        ]

    def test_creates_before_pruning_and_leaves_manual_snapshots_alone(self):
        self.seed(
            [
                ("manual-backup", 1),
                ("60-minutes-ago", 100),
                ("45-minutes-ago", 200),
                ("30-minutes-ago", 300),
                ("15-minutes-ago", 400),
                ("00-minutes-ago", 500),
            ]
        )

        result = self.run_retention("frequently", 5)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.commands(),
            [
                (
                    "snapshot",
                    "tank/videos@nas-auto-frequently-20260808T120000Z",
                ),
                ("destroy", "tank/videos@60-minutes-ago"),
            ],
        )
        self.assertIn("tank/videos@manual-backup\t1", self.state.read_text())

    def test_failed_creation_never_prunes(self):
        self.seed([("60-minutes-ago", 100)])

        result = self.run_retention("frequently", 1, fail_snapshot=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.commands(),
            [
                (
                    "snapshot",
                    "tank/videos@nas-auto-frequently-20260808T120000Z",
                )
            ],
        )
        self.assertIn("tank/videos@60-minutes-ago", self.state.read_text())

    def test_failed_listing_is_reported_after_safe_creation(self):
        result = self.run_retention("frequently", 5, fail_listing=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unable to enumerate snapshots", result.stderr)
        self.assertEqual(
            self.commands(),
            [
                (
                    "snapshot",
                    "tank/videos@nas-auto-frequently-20260808T120000Z",
                )
            ],
        )

    def test_same_timestamp_retry_resumes_after_listing_failure(self):
        first = self.run_retention("frequently", 1, fail_listing=True)
        second = self.run_retention("frequently", 1)

        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            self.commands(),
            [
                (
                    "snapshot",
                    "tank/videos@nas-auto-frequently-20260808T120000Z",
                ),
                (
                    "snapshot",
                    "tank/videos@nas-auto-frequently-20260808T120000Z",
                ),
            ],
        )

    def test_destroy_failure_leaves_extra_history_then_converges(self):
        self.seed([("60-minutes-ago", 100)])

        first = self.run_retention("frequently", 1, fail_destroy=True)
        after_failure = self.state.read_text()
        second = self.run_retention("frequently", 1)

        self.assertNotEqual(first.returncode, 0)
        self.assertIn("tank/videos@60-minutes-ago", after_failure)
        self.assertIn("nas-auto-frequently", after_failure)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("tank/videos@60-minutes-ago", self.state.read_text())

    def test_each_complete_legacy_window_converges_to_the_same_count(self):
        cases = {
            "frequently": [
                "60-minutes-ago",
                "45-minutes-ago",
                "30-minutes-ago",
                "15-minutes-ago",
                "00-minutes-ago",
            ],
            "hourly": [
                "24-hours-ago",
                *[f"{hour}-hours-ago" for hour in range(23, 1, -1)],
                "1-hour-ago",
                "0-hours-ago",
            ],
            "daily": [
                *[f"{day}-days-ago" for day in range(7, 1, -1)],
                "yesterday",
                "today",
            ],
            "weekly": [
                "4-weeks-ago",
                "3-weeks-ago",
                "2-weeks-ago",
                "last-week",
                "this-week",
            ],
            "monthly": [
                *[f"{month}-months-ago" for month in range(12, 1, -1)],
                "last-month",
                "this-month",
            ],
        }
        for cadence, legacy_names in cases.items():
            with self.subTest(cadence=cadence):
                self.seed(
                    [
                        (legacy_name, creation)
                        for creation, legacy_name in enumerate(legacy_names, 1)
                    ]
                )
                self.log.write_text("")

                result = self.run_retention(cadence, len(legacy_names))

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.commands()[-1],
                    ("destroy", f"tank/videos@{legacy_names[0]}"),
                )
                self.assertEqual(
                    len(self.state.read_text().splitlines()),
                    len(legacy_names),
                )

    def test_other_cadences_children_and_manual_snapshots_are_not_pruned(self):
        self.state.write_text(
            "tank/videos@60-minutes-ago\t100\n"
            "tank/videos@0-hours-ago\t50\n"
            "tank/videos@manual\t25\n"
            "tank/videos/child@60-minutes-ago\t1\n"
        )

        result = self.run_retention("frequently", 1)

        self.assertEqual(result.returncode, 0, result.stderr)
        remaining = self.state.read_text()
        self.assertIn("tank/videos@0-hours-ago", remaining)
        self.assertIn("tank/videos@manual", remaining)
        self.assertIn("tank/videos/child@60-minutes-ago", remaining)

    def test_missing_dataset_fails_before_snapshot_creation(self):
        result = self.run_retention("frequently", 5, dataset_exists=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands(), [])

    def test_zero_retention_means_keep_forever(self):
        self.seed([("2025-01-01", 100), ("2026-01-01", 200)])

        result = self.run_retention("yearly", 0)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.commands(),
            [("snapshot", "tank/videos@nas-auto-yearly-20260808T120000Z")],
        )
        self.assertIn("tank/videos@2025-01-01", self.state.read_text())

    def test_service_templates_declare_the_complete_retention_policy(self):
        expected = {
            "frequently": 5,
            "hourly": 25,
            "daily": 8,
            "weekly": 5,
        }
        for cadence, retention in expected.items():
            with self.subTest(cadence=cadence):
                unit = (
                    SYSTEMD / f"zfs-snapshots-{cadence}@.service"
                ).read_text()
                self.assertIn(
                    "ExecStart=/usr/local/bin/zfs-snapshot-retained.sh "
                    f"tank/%I {cadence} {retention}",
                    unit,
                )


if __name__ == "__main__":
    unittest.main()
