# ABOUTME: Guards the shared validation boundary used by image build workflows.

from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github/workflows"


class BuildWorkflowTests(unittest.TestCase):
    def test_image_workflows_use_one_reusable_preflight(self):
        preflight = (WORKFLOWS / "build-preflight.yaml").read_text()
        self.assertIn("workflow_call:", preflight)
        self.assertIn("verify-repository:", preflight)
        self.assertIn("verify-sops-image:", preflight)
        self.assertIn("query-versions:", preflight)
        self.assertEqual(preflight.count("run: make check"), 1)
        self.assertEqual(preflight.count("run: make test"), 1)
        self.assertIn("uses: astral-sh/setup-uv@", preflight)
        self.assertIn('version: "0.12.17"', preflight)
        self.assertNotIn("actions/setup-python", preflight)
        self.assertNotIn("pip install", preflight)

        for filename in ("build.yaml", "build-check.yaml"):
            with self.subTest(filename=filename):
                workflow = (WORKFLOWS / filename).read_text()
                self.assertEqual(
                    workflow.count(
                        "uses: ./.github/workflows/build-preflight.yaml"
                    ),
                    1,
                )
                self.assertIn("needs: preflight", workflow)
                self.assertIn("needs.preflight.outputs.zfs-version", workflow)
                self.assertIn("needs.preflight.outputs.kernel-version", workflow)
                self.assertIn(
                    "IMAGE_NAME: ${{ github.repository }}/bootc",
                    workflow,
                )
                self.assertNotIn("make check", workflow)
                self.assertNotIn("make test", workflow)
                self.assertNotIn("Verify SOPS image signature", workflow)
                self.assertNotIn("Resolve and verify build inputs", workflow)
                self.assertEqual(workflow.count("make verify-image"), 1)
                self.assertIn("uses: docker/setup-buildx-action@", workflow)
                self.assertIn("uses: docker/build-push-action@", workflow)
                self.assertIn("cache-from: type=gha", workflow)
                self.assertIn("cache-to: type=gha,mode=max", workflow)
                self.assertNotIn("redhat-actions/", workflow)
                self.assertNotIn("fuse-overlayfs", workflow)

    def test_production_build_pushes_and_loads_one_build_result(self):
        workflow = (WORKFLOWS / "build.yaml").read_text()

        self.assertIn("uses: docker/login-action@", workflow)
        self.assertIn("push: true", workflow)
        self.assertIn("load: true", workflow)
        self.assertIn("provenance: false", workflow)
        self.assertIn("sbom: false", workflow)

        verification = workflow.index("make verify-image")
        self.assertGreater(verification, workflow.index("Build and push container image"))
        self.assertLess(verification, workflow.index("Generate artifact attestation"))
        self.assertLess(verification, workflow.index("Sign the published OCI image"))

    def test_build_check_loads_without_pushing(self):
        workflow = (WORKFLOWS / "build-check.yaml").read_text()

        self.assertIn("load: true", workflow)
        self.assertNotIn("push: true", workflow)
        self.assertNotIn("docker/login-action", workflow)

    def test_signature_policy_exists_only_in_shared_preflight(self):
        identity = "--certificate-identity-regexp=https://github.com/getsops"
        issuer = (
            "--certificate-oidc-issuer="
            "https://token.actions.githubusercontent.com"
        )
        preflight = (WORKFLOWS / "build-preflight.yaml").read_text()
        self.assertEqual(preflight.count(identity), 1)
        self.assertEqual(preflight.count(issuer), 1)

        all_workflows = "\n".join(
            path.read_text() for path in WORKFLOWS.glob("*.yaml")
        )
        self.assertEqual(all_workflows.count(identity), 1)
        self.assertEqual(all_workflows.count(issuer), 1)

    def test_production_publisher_is_serialized_without_cancellation(self):
        workflow = (WORKFLOWS / "build.yaml").read_text()

        self.assertIn("group: nas-publisher", workflow)
        self.assertIn("cancel-in-progress: false", workflow)

    def test_cleanup_covers_current_and_legacy_packages(self):
        workflow = (WORKFLOWS / "cleanup-images.yaml").read_text()

        self.assertIn("package-name: 'nas/bootc'", workflow)
        self.assertIn("package-name: 'custom-coreos'", workflow)
        self.assertIn("CONTAINER_PACKAGE_NAME: custom-coreos", workflow)
        self.assertIn("id: bootc_versions", workflow)
        self.assertIn("id: legacy_versions", workflow)

    def test_bootc_signature_policy_is_not_a_nas_namespace_wildcard(self):
        policy = json.loads(
            (REPO / "overlay-root/etc/containers/policy.json").read_text()
        )
        docker_policy = policy["transports"]["docker"]

        self.assertIn("ghcr.io/samhclark/nas/bootc", docker_policy)
        self.assertNotIn("ghcr.io/samhclark/nas", docker_policy)
        self.assertNotIn("ghcr.io/samhclark/nas/*", docker_policy)


if __name__ == "__main__":
    unittest.main()
