# ABOUTME: Locks Immich machine-learning cache paths to declared writable storage.

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class ImmichMachineLearningTests(unittest.TestCase):
    def test_hugging_face_cache_uses_declared_writable_storage(self):
        with (REPO / "quadlets" / "immich-machine-learning.toml").open("rb") as stream:
            service = tomllib.load(stream)

        environment = service["container"]["environment"]
        self.assertEqual(environment["XDG_CACHE_HOME"], "/.cache")
        self.assertEqual(environment["XDG_CONFIG_HOME"], "/.config")
        self.assertEqual(environment["HF_HOME"], "/.cache/huggingface")
        self.assertEqual(environment["HF_XET_CACHE"], "/.cache/huggingface/xet")

        writable_exports = {
            export["container-path"]
            for storage in service["storage"]
            for export in storage.get("exports", [])
            if export["access"] == "read-write"
        }
        self.assertIn(environment["XDG_CACHE_HOME"], writable_exports)
        self.assertIn(environment["XDG_CONFIG_HOME"], writable_exports)


if __name__ == "__main__":
    unittest.main()
