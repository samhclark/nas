# ABOUTME: Verifies the image-controlled *arr and SABnzbd entrypoint contracts.

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ENTRYPOINT_ROOT = REPO / "overlay-root/usr/share/nas"
SAB_HELPER = ENTRYPOINT_ROOT / "sabnzbd/ensure-host-whitelist.py"
SAB_HOSTNAMES = "sabnzbd.i.samhclark.com,sabnzbd.krun"
SAB_IMAGE = (
    "lscr.io/linuxserver/sabnzbd:5.1.0-ls266@"
    "sha256:b0f9755d795913bd26ae3f3a12805668ab0681ab847a7624568559c573fc7cae"
)


def _host_helper_available() -> bool:
    if os.geteuid() != 1000 or os.getegid() != 1000:
        return False
    try:
        import configobj  # noqa: F401
    except ImportError:
        return False
    return True


def _image_helper_available() -> bool:
    podman = shutil.which("podman")
    if podman is None:
        return False
    result = subprocess.run(
        [podman, "image", "exists", SAB_IMAGE],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


HELPER_RUNNER = (
    "host" if _host_helper_available() else "image" if _image_helper_available() else None
)


SERVICES = {
    "sonarr": {
        "script": ENTRYPOINT_ROOT / "sonarr/sonarr-entrypoint.sh",
        "command": "/app/sonarr/bin/Sonarr -nobrowser -data=/config",
    },
    "radarr": {
        "script": ENTRYPOINT_ROOT / "radarr/radarr-entrypoint.sh",
        "command": "/app/radarr/bin/Radarr -nobrowser -data=/config",
    },
    "prowlarr": {
        "script": ENTRYPOINT_ROOT / "prowlarr/prowlarr-entrypoint.sh",
        "command": "/app/prowlarr/bin/Prowlarr -nobrowser -data=/config",
    },
}

class ArrEntrypointContractTests(unittest.TestCase):
    def setUp(self):
        if self._testMethodName.startswith("test_sabnzbd_helper") and HELPER_RUNNER is None:
            self.skipTest(
                "ConfigObj is unavailable and the pinned SAB image is not local; "
                "run the opt-in image smoke with the image available"
            )

    def test_dotnet_adapters_have_the_required_identity_and_command_contract(self):
        for service, contract in SERVICES.items():
            with self.subTest(service=service):
                script = contract["script"].read_text()
                mode = contract["script"].stat().st_mode

                self.assertTrue(mode & stat.S_IXUSR)
                self.assertIn("set -euo pipefail", script)
                self.assertIn("umask 002", script)
                self.assertNotIn("groupmod", script)
                self.assertNotIn("usermod", script)
                self.assertIn("exec s6-setuidgid 1000:1000", script)
                self.assertIn("expected root or 1000:1000", script)
                self.assertIn(contract["command"], script)
                self.assertIn('"$@"', script)
                self.assertNotIn("temp_dir", script)
                self.assertNotIn("/run/", script)
                self.assertNotIn("mkdir", script)
                self.assertNotIn("chown", script)
                self.assertNotIn("chown -R", script)

    def test_sabnzbd_has_the_family_selection_and_no_temp_directory(self):
        script = (
            ENTRYPOINT_ROOT / "sabnzbd/sabnzbd-entrypoint.sh"
        ).read_text()

        self.assertIn("if [[ -e /proc/net/if_inet6 ]]; then", script)
        self.assertIn('readonly family="::"', script)
        self.assertIn('readonly family="0.0.0.0"', script)
        self.assertIn(
            "python3 /app/sabnzbd/SABnzbd.py --config-file /config",
            script,
        )
        self.assertIn('--server "${family}" --console "$@"', script)
        self.assertIn("umask 002", script)
        self.assertNotIn("umask 022", script)
        self.assertIn("ensure-host-whitelist.py", script)
        self.assertNotIn("mkdir", script)
        self.assertNotIn("chown", script)

    def test_root_mode_handoffs_numerically_without_mutating_run(self):
        for service, contract in SERVICES.items():
            with self.subTest(service=service):
                log, result = self._run_with_fake_root_commands(
                    contract["script"],
                    uid="0",
                    gid="0",
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("groupmod", log)
                self.assertNotIn("usermod", log)
                self.assertIn(
                    f"s6-setuidgid 1000:1000 {contract['command']} --test-flag value",
                    log,
                )
                self.assertNotIn("mkdir", log)
                self.assertNotIn("chown", log)
                self.assertNotIn("chown -R", log)

    def test_sabnzbd_root_mode_uses_the_host_ipv6_family_signal(self):
        script = ENTRYPOINT_ROOT / "sabnzbd/sabnzbd-entrypoint.sh"
        log, result = self._run_with_fake_root_commands(
            script,
            uid="0",
            gid="0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"s6-setuidgid 1000:1000 {script} --test-flag value",
            log,
        )

    def test_sabnzbd_helper_creates_config_with_required_hostnames(self):
        result, content, mode = self._run_sab_helper(None)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            content,
            "[misc]\n"
            "host_whitelist = sabnzbd.i.samhclark.com, sabnzbd.krun\n"
            "permissions = 2770\n",
        )
        self.assertEqual(mode, 0o600)

    def test_sabnzbd_helper_preserves_existing_entries(self):
        result, content, _ = self._run_sab_helper(
            "# keep this comment\n__version__ = 16\n[misc]\n"
            "host_whitelist = nas,\n"
            "description = \"\"\"line one\nline two\n\"\"\"\n"
            "note = value # keep this comment\n"
            "[servers]\n[[provider]]\nname = retained\n"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("# keep this comment", content)
        self.assertIn("__version__ = 16", content)
        self.assertIn(
            "host_whitelist = nas, sabnzbd.i.samhclark.com, sabnzbd.krun", content
        )
        self.assertIn("permissions = 2770", content)
        self.assertIn("description = '''line one", content)
        self.assertIn("line two", content)
        self.assertIn("# keep this comment", content)
        self.assertIn("[servers]", content)
        self.assertIn("[[provider]]", content)
        self.assertIn("name = retained", content)

    def test_sabnzbd_helper_is_idempotent_and_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "sabnzbd.ini"
            config.write_text(
                "__version__ = 16\n"
                "[misc]\nhost_whitelist = nas,sabnzbd.krun\n"
                "[servers]\n[[provider]]\nname = provider\n"
            )
            config.chmod(0o640)
            first = self._invoke_sab_helper(config)
            first_mtime = config.stat().st_mtime_ns
            second = self._invoke_sab_helper(config)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn(
                "host_whitelist = nas, sabnzbd.krun, sabnzbd.i.samhclark.com",
                config.read_text(),
            )
            self.assertIn("permissions = 2770", config.read_text())
            self.assertIn("__version__ = 16", config.read_text())
            self.assertIn("[[provider]]", config.read_text())
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o640)
            self.assertEqual(config.stat().st_mtime_ns, first_mtime)

    def test_sabnzbd_helper_replaces_restrictive_completed_permissions(self):
        result, content, _ = self._run_sab_helper(
            "[misc]\nhost_whitelist = nas\npermissions = 0700\n"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("host_whitelist = nas, sabnzbd.i.samhclark.com, sabnzbd.krun", content)
        self.assertIn("permissions = 2770", content)
        self.assertNotIn("permissions = 0700", content)

    def test_sabnzbd_helper_rejects_malformed_config_without_replacing_it(self):
        result, content, _ = self._run_sab_helper(
            "[misc]\nthis is not an assignment\n"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot parse", result.stderr)
        self.assertEqual(content, "[misc]\nthis is not an assignment\n")

    def test_sabnzbd_helper_updates_backup_without_creating_main(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            config = directory / "sabnzbd.ini"
            backup = directory / "sabnzbd.ini.bak"
            backup.write_text(
                "__version__ = 16\n[misc]\nhost_whitelist = nas,\n"
                "[servers]\n[[provider]]\nname = retained\n"
            )
            backup.chmod(0o640)
            result = self._invoke_sab_helper(config)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(config.exists())
            self.assertIn(
                "host_whitelist = nas, sabnzbd.i.samhclark.com, sabnzbd.krun",
                backup.read_text(),
            )
            self.assertIn("[[provider]]", backup.read_text())
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o640)

    def test_sabnzbd_helper_rejects_symlink_without_replacing_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            target = directory / "real.ini"
            target.write_text("[misc]\nhost_whitelist = nas\n")
            config = directory / "sabnzbd.ini"
            config.symlink_to(target)
            result = self._invoke_sab_helper(config)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(), "[misc]\nhost_whitelist = nas\n")

    def test_sabnzbd_helper_rejects_malformed_backup_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            config = directory / "sabnzbd.ini"
            backup = directory / "sabnzbd.ini.bak"
            original = "[misc]\nthis is not an assignment\n"
            backup.write_text(original)
            result = self._invoke_sab_helper(config)

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(config.exists())
            self.assertEqual(backup.read_text(), original)

    def test_unexpected_identity_is_rejected_before_launch(self):
        script = ENTRYPOINT_ROOT / "sonarr/sonarr-entrypoint.sh"
        log, result = self._run_with_fake_root_commands(
            script,
            uid="1234",
            gid="1234",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported effective identity 1234:1234", result.stderr)
        self.assertEqual(log, "")

    def test_uid_1000_with_the_wrong_gid_is_rejected(self):
        script = ENTRYPOINT_ROOT / "radarr/radarr-entrypoint.sh"
        log, result = self._run_with_fake_root_commands(
            script,
            uid="1000",
            gid="1001",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UID 1000 requires GID 1000", result.stderr)
        self.assertEqual(log, "")

    @staticmethod
    def _run_with_fake_root_commands(
        script: Path,
        *,
        uid: str,
        gid: str,
    ) -> tuple[str, subprocess.CompletedProcess[str]]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            log_path = temp / "commands.log"

            (fake_bin / "id").write_text(
                "#!/usr/bin/env bash\n"
                'case "$1" in\n'
                f'    -u) printf "%s\\n" "{uid}" ;;\n'
                f'    -g) printf "%s\\n" "{gid}" ;;\n'
                "esac\n"
            )
            (fake_bin / "mkdir").write_text(
                "#!/usr/bin/env bash\n"
                f'printf "mkdir %s\\n" "$*" >> "{log_path}"\n'
            )
            (fake_bin / "chown").write_text(
                "#!/usr/bin/env bash\n"
                f'printf "chown %s\\n" "$*" >> "{log_path}"\n'
            )
            (fake_bin / "s6-setuidgid").write_text(
                "#!/usr/bin/env bash\n"
                f'printf "s6-setuidgid %s\\n" "$*" >> "{log_path}"\n'
            )
            for command in fake_bin.iterdir():
                command.chmod(0o755)

            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

            result = subprocess.run(
                [str(script), "--test-flag", "value"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            log = log_path.read_text() if log_path.exists() else ""
            return log, result

    @staticmethod
    def _invoke_sab_helper(config: Path) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["NAS_SABNZBD_ALLOWED_HOSTNAMES"] = SAB_HOSTNAMES
        if HELPER_RUNNER == "host":
            command = ["python3", str(SAB_HELPER), str(config)]
        else:
            helper_copy = config.parent / ".ensure-host-whitelist.py"
            shutil.copy2(SAB_HELPER, helper_copy)
            podman = shutil.which("podman")
            assert podman is not None
            command = [
                podman,
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--user=1000:1000",
                "--userns=keep-id:uid=1000,gid=1000",
                "--env",
                f"NAS_SABNZBD_ALLOWED_HOSTNAMES={SAB_HOSTNAMES}",
                "--volume",
                f"{config.parent}:/test:rw,Z",
                "--entrypoint",
                "python3",
                SAB_IMAGE,
                "/test/.ensure-host-whitelist.py",
                f"/test/{config.name}",
            ]
        return subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_sab_helper(
        self,
        initial: str | None,
    ) -> tuple[subprocess.CompletedProcess[str], str, int]:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "sabnzbd.ini"
            if initial is not None:
                config.write_text(initial)
            result = self._invoke_sab_helper(config)
            content = config.read_text()
            mode = stat.S_IMODE(config.stat().st_mode)
            return result, content, mode
