# ABOUTME: Keeps Python development reproducible and self-bootstrapping through uv.

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class DevelopmentToolchainTests(unittest.TestCase):
    def test_uv_is_the_only_python_dependency_boundary(self):
        project = tomllib.loads((REPO / "pyproject.toml").read_text())

        self.assertEqual(project["project"]["requires-python"], ">=3.14")
        self.assertEqual(project["dependency-groups"]["dev"], ["ty==0.0.69"])
        self.assertFalse(project["tool"]["uv"]["package"])
        self.assertEqual(project["tool"]["uv"]["required-version"], "==0.12.9")
        self.assertTrue((REPO / "uv.lock").is_file())
        self.assertFalse((REPO / "requirements-dev.txt").exists())

    def test_make_runs_python_tools_in_the_locked_uv_environment(self):
        makefile = (REPO / "Makefile").read_text()

        self.assertIn("UV_RUN       := $(UV) run --locked", makefile)
        self.assertIn("$(UV_RUN) ty check", makefile)
        self.assertIn("$(UV_RUN) python -m unittest", makefile)
        self.assertIn("$(UV_RUN) python generate-quadlets.py", makefile)
        self.assertNotIn("$(PYTHON)", makefile)

    def test_make_build_uses_buildx_and_the_named_containerfile(self):
        makefile = (REPO / "Makefile").read_text()

        self.assertIn(
            "$(DOCKER) buildx build --file Containerfile --load --pull",
            makefile,
        )
        self.assertIn('CONTAINER_CLI="$(DOCKER)"', makefile)
        self.assertNotIn("$(PODMAN) build", makefile)

    def test_immich_image_preflight_runs_smoke_before_krun_probe(self):
        makefile = (REPO / "Makefile").read_text()
        lines = makefile.splitlines()
        target_index = next(
            index
            for index, line in enumerate(lines)
            if re.match(r"^preflight-immich-images\s*:", line)
        )

        recipe_lines = []
        for line in lines[target_index + 1 :]:
            if line.startswith("\t"):
                recipe_lines.append(line)
            elif line.strip():
                break

        invocations = [
            re.search(r"\$\(MAKE\).*\b(smoke-immich-images|probe-krun-user)\b", line)
            for line in recipe_lines
        ]
        invocations = [match.group(1) for match in invocations if match]

        self.assertEqual(
            invocations,
            ["smoke-immich-images", "probe-krun-user"],
        )


if __name__ == "__main__":
    unittest.main()
