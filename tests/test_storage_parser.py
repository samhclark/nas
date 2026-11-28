# ABOUTME: Exercises the closed typed parser for declarative service storage.
# ABOUTME: Guards non-destructive lifecycle and explicit ZFS property contracts.

from __future__ import annotations

import tomllib
import unittest
from typing import cast

from quadletgen.model import ConfigError
from quadletgen.storage_model import (
    DirectoryStorage,
    ManagedZfsStorage,
    StorageAccess,
    StorageExport,
    ZfsCompression,
    ZfsPrimaryCache,
    ZfsRecordSize,
)
from quadletgen.storage_parser import parse_storage


DIRECTORY = """
[[storage]]
name = "state"
kind = "directory"
host-path = "/var/lib/example"
mode = "0750"
subdirectories = ["cache", "state/db"]

[[storage.exports]]
subpath = "."
container-path = "/var/lib/example"
access = "read-write"

[[storage.exports]]
subpath = "cache"
container-path = "/cache"
access = "read-write"
"""

MANAGED_ZFS = """
[[storage]]
name = "metadata"
kind = "managed-zfs"
dataset = "tank/example/metadata"
host-path = "/var/lib/example/metadata"
mode = "0750"
record-size = "4K"
compression = "lz4"
atime = false
primary-cache = "metadata"

[[storage.exports]]
subpath = "."
container-path = "/metadata"
access = "read-write"
"""

EXISTING_ZFS = """
[[storage]]
name = "media"
kind = "existing-zfs"
dataset = "tank/videos"
host-path = "/var/zfs/tank/videos"

[[storage.exports]]
subpath = "movies"
container-path = "/media/movies"
access = "read-only"

[[storage.exports]]
subpath = "tv-shows"
container-path = "/media/tv-shows"
access = "read-only"
"""


class StorageParserTests(unittest.TestCase):
    def parse(self, source: str):
        raw = tomllib.loads(source)
        return parse_storage(raw.get("storage"), "example.toml")

    def assert_invalid(self, source: str, message: str) -> None:
        with self.assertRaisesRegex(ConfigError, message) as raised:
            self.parse(source)
        self.assertTrue(str(raised.exception).startswith("example.toml:"))

    def test_parses_each_discriminated_storage_kind(self):
        directory, managed = self.parse(DIRECTORY + MANAGED_ZFS)

        self.assertIsInstance(directory, DirectoryStorage)
        self.assertEqual(directory.kind, "directory")
        self.assertEqual(directory.subdirectories, ("cache", "state/db"))
        self.assertEqual(directory.exports[0].access, StorageAccess.READ_WRITE)

        self.assertIsInstance(managed, ManagedZfsStorage)
        self.assertEqual(managed.kind, "managed-zfs")
        self.assertEqual(managed.record_size, ZfsRecordSize.DATABASE)
        self.assertEqual(managed.compression, ZfsCompression.LZ4)
        self.assertFalse(managed.atime)
        self.assertEqual(managed.primary_cache, ZfsPrimaryCache.METADATA)

    def test_absent_storage_section_is_an_empty_contract(self):
        self.assertEqual(parse_storage(None, "example.toml"), ())

    def test_rejects_non_table_arrays_and_unknown_kinds(self):
        with self.assertRaisesRegex(ConfigError, "array of tables"):
            parse_storage({}, "example.toml")
        self.assert_invalid(
            DIRECTORY.replace('kind = "directory"', 'kind = "volume"'),
            "managed-zfs",
        )

    def test_each_kind_has_a_closed_schema(self):
        self.assert_invalid(
            DIRECTORY.replace('mode = "0750"', 'mode = "0750"\ndataset = "tank/x"'),
            "unknown keys: dataset",
        )
        self.assert_invalid(
            MANAGED_ZFS.replace(
                'primary-cache = "metadata"',
                'primary-cache = "metadata"\ndedup = true',
            ),
            "unknown keys: dedup",
        )

    def test_destructive_lifecycle_controls_are_not_part_of_the_language(self):
        self.assert_invalid(
            MANAGED_ZFS.replace(
                'kind = "managed-zfs"',
                'kind = "managed-zfs"\nlifecycle = "replace"',
            ),
            "unknown keys: lifecycle",
        )
        self.assertNotIn("lifecycle", ManagedZfsStorage.__dataclass_fields__)
        self.assertNotIn("destroy", ManagedZfsStorage.__dataclass_fields__)

    def test_managed_zfs_requires_every_explicit_property(self):
        fields = {
            "record-size": 'record-size = "4K"\n',
            "compression": 'compression = "lz4"\n',
            "atime": "atime = false\n",
            "primary-cache": 'primary-cache = "metadata"\n',
        }
        for name, declaration in fields.items():
            with self.subTest(name=name):
                self.assert_invalid(
                    MANAGED_ZFS.replace(declaration, ""),
                    f"missing '{name}'",
                )

    def test_managed_zfs_properties_are_typed_and_closed(self):
        cases = {
            "record-size": ('record-size = "4K"', 'record-size = "12K"'),
            "compression": (
                'compression = "lz4"',
                'compression = "gzip"',
            ),
            "atime": ("atime = false", 'atime = "off"'),
            "primary-cache": (
                'primary-cache = "metadata"',
                'primary-cache = "none"',
            ),
        }
        for name, (valid, invalid) in cases.items():
            with self.subTest(name=name):
                self.assert_invalid(
                    MANAGED_ZFS.replace(valid, invalid),
                    name,
                )

    def test_postgresql_record_size_is_supported(self):
        (managed,) = self.parse(
            MANAGED_ZFS.replace('record-size = "4K"', 'record-size = "32K"')
        )
        self.assertEqual(managed.record_size, ZfsRecordSize.POSTGRESQL)

    def test_exports_are_explicit_typed_and_unique(self):
        self.assert_invalid(
            DIRECTORY.replace('access = "read-write"', 'access = "write"'),
            "read-only",
        )
        self.assert_invalid(
            DIRECTORY.replace(
                'container-path = "/cache"',
                'container-path = "/var/lib/example"',
            ),
            "repeat a container path",
        )
        self.assert_invalid(
            DIRECTORY.replace('subpath = "cache"', 'subpath = "missing"'),
            "declared subdirectory",
        )

    def test_managed_dataset_exports_only_its_root(self):
        self.assert_invalid(
            MANAGED_ZFS.replace('subpath = "."', 'subpath = "data"'),
            "dataset root",
        )

    def test_service_storage_rejects_existing_zfs(self):
        self.assert_invalid(
            EXISTING_ZFS,
            r'\[\[storage\]\]\[1\]\.kind: must be "directory" or "managed-zfs"',
        )

    def test_paths_datasets_modes_and_names_are_strict(self):
        cases = {
            "host path": (
                DIRECTORY.replace(
                    'host-path = "/var/lib/example"',
                    'host-path = "/srv/example"',
                ),
                "below /var",
            ),
            "container path": (
                DIRECTORY.replace(
                    'container-path = "/cache"',
                    'container-path = "../cache"',
                ),
                "normalized absolute path",
            ),
            "dataset": (
                MANAGED_ZFS.replace(
                    'dataset = "tank/example/metadata"',
                    'dataset = "tank"',
                ),
                "below a pool",
            ),
            "mode": (
                DIRECTORY.replace('mode = "0750"', 'mode = "750"'),
                "four-digit octal",
            ),
            "name": (
                DIRECTORY.replace('name = "state"', 'name = "State!"'),
                "must match",
            ),
        }
        for name, (source, message) in cases.items():
            with self.subTest(name=name):
                self.assert_invalid(source, message)

    def test_direct_construction_enforces_the_model_boundary(self):
        with self.assertRaisesRegex(ConfigError, "container-path"):
            StorageExport(".", "relative", StorageAccess.READ_ONLY)
        with self.assertRaisesRegex(ConfigError, "StorageExport"):
            DirectoryStorage(
                name="state",
                host_path="/var/lib/example",
                mode="0750",
                subdirectories=(),
                exports=cast(
                    tuple[StorageExport, ...],
                    ("not-an-export",),
                ),
            )

    def test_collection_rejects_ambiguous_cross_storage_identity(self):
        duplicate_name = MANAGED_ZFS.replace('name = "metadata"', 'name = "state"')
        self.assert_invalid(DIRECTORY + duplicate_name, "duplicate names")

        duplicate_target = MANAGED_ZFS.replace(
            'container-path = "/metadata"',
            'container-path = "/cache"',
        )
        self.assert_invalid(DIRECTORY + duplicate_target, "duplicate container paths")


if __name__ == "__main__":
    unittest.main()
