# ABOUTME: Exercises trusted-checkout Quadlet image-pin regeneration.

from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / ".github/scripts/renovate-quadlets.py"
PINS = {
    "alpha": "registry.example/alpha:1.0@sha256:" + "a" * 64,
    "beta": "registry.example/beta:2.0@sha256:" + "b" * 64,
}


class RenovateQuadletsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.output = self.repo.parent / f"{self.repo.name}-additions.json"
        self.addCleanup(lambda: self.output.unlink(missing_ok=True))
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "Makefile").write_text(
            "generate-quadlets:\n\t@python3 fixture.py\n"
        )
        (self.repo / "fixture.py").write_text(
            """from pathlib import Path
import tomllib

for source in Path("quadlets").glob("*.toml"):
    service = tomllib.loads(source.read_text())
    name = service["service"]["name"]
    uid = service["host"]["uid"]
    image = service["container"]["image"]
    output = Path(
        f"overlay-root/etc/containers/systemd/users/{uid}/{name}.container"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"Image={image}\\n")
"""
        )
        (self.repo / "quadlets").mkdir()
        for uid, (name, image) in enumerate(PINS.items(), start=51001):
            (self.repo / f"quadlets/{name}.toml").write_text(
                f'[service]\nname="{name}"\n[host]\nuid={uid}\n'
                f'[container]\nimage="{image}"\n'
            )
        self.make()
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True,
            text=True, timeout=5,
        )

    def make(self):
        subprocess.run(
            ["make", "generate-quadlets"], cwd=self.repo, check=True,
            capture_output=True, text=True, timeout=5,
        )

    def candidate(self):
        self.git("reset", "--hard", self.base)
        self.git("checkout", "-qB", "candidate", self.base)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "candidate")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def invoke(self, head):
        self.git("checkout", "--detach", "-q", self.base)
        self.output.unlink(missing_ok=True)
        result = subprocess.run(
            ["python3", str(HELPER), head, str(self.output)], cwd=self.repo,
            capture_output=True, text=True, timeout=10,
        )
        return result, self.output

    def additions(self, output):
        return {
            row["path"]: base64.b64decode(row["contents"]).decode()
            for row in json.loads(output.read_text())
        }

    def test_single_and_grouped_tag_or_digest_pins_generate_files(self):
        cases = {
            "single tag": {"alpha": PINS["alpha"].replace(":1.0@", ":1.1@")},
            "grouped digests": {
                name: pin[:-64] + digest * 64
                for (name, pin), digest in zip(PINS.items(), ("c", "d"))
            },
            "tag and digest": {
                "alpha": PINS["alpha"].replace(":1.0@", ":1.2@").replace("a" * 64, "e" * 64)
            },
        }
        for label, updates in cases.items():
            with self.subTest(label=label):
                self.candidate()
                for name, image in updates.items():
                    path = self.repo / f"quadlets/{name}.toml"
                    old = PINS[name]
                    path.write_text(path.read_text().replace(old, image))
                head = self.commit()
                result, output = self.invoke(head)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(output.exists(), result.stdout + result.stderr)
                files = self.additions(output)
                self.assertEqual(len(files), len(updates))
                for name, image in updates.items():
                    uid = 51001 if name == "alpha" else 51002
                    path = f"overlay-root/etc/containers/systemd/users/{uid}/{name}.container"
                    self.assertEqual(files[path], f"Image={image}\n")

    def test_completed_unrelated_mixed_mode_and_repository_changes_skip(self):
        for case in ("completed", "unrelated", "mixed", "mode", "repository"):
            with self.subTest(case=case):
                self.candidate()
                path = self.repo / "quadlets/alpha.toml"
                text = path.read_text().replace(":1.0@", ":1.1@")
                if case == "mixed":
                    text = text.replace("uid=51001", "uid=51009")
                if case == "repository":
                    text = text.replace("registry.example/alpha", "mirror.example/alpha")
                path.write_text(text)
                if case == "completed":
                    self.make()
                elif case == "unrelated":
                    (self.repo / "README.md").write_text("other change\n")
                elif case == "mode":
                    path.chmod(0o755)
                head = self.commit()
                result, output = self.invoke(head)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
