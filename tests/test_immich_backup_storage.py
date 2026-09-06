# ABOUTME: Exercises backup storage preparation without modifying host BPF or SELinux state.

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PREPARER = (
    Path(__file__).resolve().parents[1]
    / "overlay-root/usr/local/bin/nas-prepare-immich-backup-storage"
)
CONTEXT = "system_u:object_r:container_file_t:s0"


class ImmichBackupStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use namespace root to exercise the real EUID guard without host privilege.
        cls.prefix = []
        if os.geteuid() != 0:
            if shutil.which("unshare") is None:
                raise unittest.SkipTest("unshare is required for namespace root")
            cls.prefix = ["unshare", "--user", "--map-root-user"]
            probe = subprocess.run(cls.prefix + ["true"], capture_output=True, text=True)
            if probe.returncode:
                raise unittest.SkipTest(f"user namespaces unavailable: {probe.stderr.strip()}")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.repository = self.directory / "repository"
        self.repository.mkdir()
        self.bpf = self.directory / "bpf"
        self.bpf.mkdir()
        self.crun = self.bpf / "crun"
        stat = self.directory / "stat"
        stat.write_text(
            '#!/bin/bash\nset -eu\n'
            'case "$*" in\n'
            '  "-f -c %T -- $NAS_BACKUP_BPF_ROOT") printf "%s\\n" "$FAKE_FS_TYPE" ;;\n'
            '  "-c %C -- $NAS_BACKUP_REPOSITORY") printf "%s\\n" "$FAKE_CONTEXT" ;;\n'
            '  *) exit 2 ;;\nesac\n'
        )
        stat.chmod(0o755)
        matchpathcon = self.directory / "matchpathcon"
        matchpathcon.write_text('#!/bin/bash\nprintf "%s\\n" "$FAKE_CONTEXT"\n')
        matchpathcon.chmod(0o755)
        self.env = {
            **os.environ,
            "NAS_BACKUP_REPOSITORY": str(self.repository),
            "NAS_BACKUP_BPF_ROOT": str(self.bpf),
            "NAS_BACKUP_CRUN_BPF_DIRECTORY": str(self.crun),
            "STAT_BIN": str(stat),
            "MATCHPATHCON_BIN": str(matchpathcon),
            "SEMANAGE_BIN": "/usr/bin/false",
            "RESTORECON_BIN": "/usr/bin/false",
            "MKDIR_BIN": "/usr/bin/mkdir",
            "FAKE_FS_TYPE": "bpf_fs",
            "FAKE_CONTEXT": CONTEXT,
        }

    def run_preparer(self):
        return subprocess.run(
            self.prefix + ["bash", str(PREPARER)],
            env=self.env, capture_output=True, text=True, timeout=10,
        )

    def test_bpffs_creates_directory_and_preserves_existing_pins_on_rerun(self):
        result = self.run_preparer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.crun.is_dir())
        self.assertEqual(self.crun.stat().st_mode & 0o777, 0o755)
        pin = self.crun / "existing-pin"
        pin.write_text("preserve")
        result = self.run_preparer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(pin.read_text(), "preserve")

    def test_other_filesystem_is_rejected_before_directory_creation(self):
        for filesystem in ("sysfs", "tmpfs"):
            with self.subTest(filesystem=filesystem):
                self.env["FAKE_FS_TYPE"] = filesystem
                result = self.run_preparer()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"not mounted as bpffs (found {filesystem})", result.stderr)
                self.assertFalse(self.crun.exists())

    def test_directory_creation_failure_is_reported(self):
        self.crun.write_text("obstruction")
        result = self.run_preparer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unable to create", result.stderr)
        self.assertEqual(self.crun.read_text(), "obstruction")


if __name__ == "__main__":
    unittest.main()
