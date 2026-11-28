# ABOUTME: Defines the closed, non-destructive storage contract for services.
# ABOUTME: Keeps directory and ZFS lifecycle semantics explicit and typed.

"""Typed service-storage declarations and their local invariants."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, NoReturn, TypeAlias

from .errors import ConfigError


NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
PORTABLE_ABSOLUTE_PATH_RE = re.compile(
    r"^/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$"
)
PORTABLE_RELATIVE_PATH_RE = re.compile(
    r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$"
)
DATASET_RE = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)+$")
FILE_MODE_RE = re.compile(r"^0[0-7]{3}$")
SHARED_FILE_MODE_RE = re.compile(r"^2[0-7]{3}$")


class StorageAccess(str, Enum):
    """A container's declared access to one storage export."""

    READ_ONLY = "read-only"
    READ_WRITE = "read-write"


class ZfsRecordSize(str, Enum):
    """Record sizes deliberately supported by the production storage policy."""

    DATABASE = "4K"
    APPLICATION = "16K"
    POSTGRESQL = "32K"
    BULK = "128K"
    OBJECT = "1M"


class ZfsCompression(str, Enum):
    """Compression modes deliberately supported by managed datasets."""

    OFF = "off"
    LZ4 = "lz4"


class ZfsPrimaryCache(str, Enum):
    """ARC content policies deliberately supported by managed datasets."""

    ALL = "all"
    METADATA = "metadata"


@dataclass(frozen=True, slots=True)
class StorageExport:
    """One path below a storage root exported into a container."""

    subpath: str
    container_path: str
    access: StorageAccess

    def __post_init__(self) -> None:
        _validate_subpath(self.subpath, "storage.export.subpath")
        _validate_absolute_path(
            self.container_path,
            "storage.export.container-path",
        )
        if not isinstance(self.access, StorageAccess):
            _fail(
                "storage.export.access",
                'must be "read-only" or "read-write"',
            )


@dataclass(frozen=True, slots=True)
class DirectoryStorage:
    """A service-owned mutable directory below ``/var``."""

    name: str
    host_path: str
    mode: str
    subdirectories: tuple[str, ...]
    exports: tuple[StorageExport, ...]
    kind: Literal["directory"] = field(default="directory", init=False)

    def __post_init__(self) -> None:
        path = _validate_common(self.name, self.host_path, self.exports)
        _validate_mode(self.mode, f"{path}.mode")
        _validate_subdirectories(self.subdirectories, path)
        declared_paths = {".", *self.subdirectories}
        for index, export in enumerate(self.exports, start=1):
            if export.subpath not in declared_paths:
                _fail(
                    f"{path}.exports[{index}].subpath",
                    "must be the storage root or a declared subdirectory",
                )


@dataclass(frozen=True, slots=True)
class ManagedZfsStorage:
    """A create-if-absent ZFS dataset with a closed property contract."""

    name: str
    dataset: str
    host_path: str
    mode: str
    record_size: ZfsRecordSize
    compression: ZfsCompression
    atime: bool
    primary_cache: ZfsPrimaryCache
    exports: tuple[StorageExport, ...]
    kind: Literal["managed-zfs"] = field(default="managed-zfs", init=False)

    def __post_init__(self) -> None:
        path = _validate_common(self.name, self.host_path, self.exports)
        _validate_dataset(self.dataset, f"{path}.dataset")
        _validate_mode(self.mode, f"{path}.mode")
        if not isinstance(self.record_size, ZfsRecordSize):
            _fail(f"{path}.record-size", "is not a supported record size")
        if not isinstance(self.compression, ZfsCompression):
            _fail(f"{path}.compression", "is not a supported compression mode")
        if type(self.atime) is not bool:
            _fail(f"{path}.atime", "must be a boolean")
        if not isinstance(self.primary_cache, ZfsPrimaryCache):
            _fail(f"{path}.primary-cache", "is not a supported cache policy")
        for index, export in enumerate(self.exports, start=1):
            if export.subpath != ".":
                _fail(
                    f"{path}.exports[{index}].subpath",
                    "managed ZFS exports must use the dataset root",
                )


@dataclass(frozen=True, slots=True)
class FleetZfsStorage:
    """A required-existing ZFS resource shared by opted-in services.

    Fleet resources are deliberately validation-only.  They describe an
    operator-owned dataset and its already-created directory layout; the
    runtime never creates, relabels, or changes ownership of this resource.
    """

    name: str
    dataset: str
    host_path: str
    shared_group: str
    mode: str
    required_paths: tuple[str, ...]
    kind: Literal["existing-zfs"] = field(default="existing-zfs", init=False)

    def __post_init__(self) -> None:
        path = f"fleet-storage[{self.name}]"
        if not isinstance(name := self.name, str) or not NAME_RE.fullmatch(name):
            _fail(f"{path}.name", f"must match {NAME_RE.pattern}")
        _validate_dataset(self.dataset, f"{path}.dataset")
        _validate_absolute_path(self.host_path, f"{path}.host-path")
        if not self.host_path.startswith("/var/"):
            _fail(f"{path}.host-path", "must be below /var")
        if not isinstance(self.shared_group, str) or not NAME_RE.fullmatch(
            self.shared_group
        ):
            _fail(f"{path}.shared-group", f"must match {NAME_RE.pattern}")
        if not isinstance(self.mode, str) or not SHARED_FILE_MODE_RE.fullmatch(
            self.mode
        ):
            _fail(f"{path}.mode", "must be a four-digit shared-directory mode")
        if self.mode != "2775":
            _fail(f"{path}.mode", 'must be "2775" for shared media storage')
        if not isinstance(self.required_paths, tuple) or not self.required_paths:
            _fail(f"{path}.required-paths", "must contain at least one path")
        for index, required_path in enumerate(self.required_paths, start=1):
            _validate_subpath(
                required_path,
                f"{path}.required-paths[{index}]",
            )
            if required_path == ".":
                _fail(f"{path}.required-paths[{index}]", 'cannot be "."')
        if len(set(self.required_paths)) != len(self.required_paths):
            _fail(f"{path}.required-paths", "cannot contain duplicates")


@dataclass(frozen=True, slots=True)
class SharedStorageExport:
    """One service's named export from a fleet-owned storage resource."""

    resource: str
    subpath: str
    container_path: str
    access: StorageAccess

    def __post_init__(self) -> None:
        if not isinstance(self.resource, str) or not NAME_RE.fullmatch(
            self.resource
        ):
            _fail("shared-storage.resource", f"must match {NAME_RE.pattern}")
        _validate_subpath(self.subpath, "shared-storage.subpath")
        _validate_absolute_path(
            self.container_path,
            "shared-storage.container-path",
        )
        if not isinstance(self.access, StorageAccess):
            _fail(
                "shared-storage.access",
                'must be "read-only" or "read-write"',
            )


StorageSpec: TypeAlias = DirectoryStorage | ManagedZfsStorage
FleetStorageSpec: TypeAlias = FleetZfsStorage


def _fail(path: str, message: str) -> NoReturn:
    raise ConfigError(f"{path}: {message}")


def _validate_common(
    name: str,
    host_path: str,
    exports: tuple[StorageExport, ...],
) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        _fail("storage.name", f"must match {NAME_RE.pattern}")
    path = f"storage[{name}]"
    _validate_absolute_path(host_path, f"{path}.host-path")
    if not host_path.startswith("/var/"):
        _fail(f"{path}.host-path", "must be below /var")
    if not isinstance(exports, tuple) or not exports:
        _fail(f"{path}.exports", "must contain at least one export")
    if any(not isinstance(export, StorageExport) for export in exports):
        _fail(f"{path}.exports", "must contain only StorageExport instances")
    targets = [export.container_path for export in exports]
    if len(set(targets)) != len(targets):
        _fail(f"{path}.exports", "cannot repeat a container path")
    return path


def _validate_absolute_path(value: str, path: str) -> None:
    if (
        not isinstance(value, str)
        or not PORTABLE_ABSOLUTE_PATH_RE.fullmatch(value)
        or posixpath.normpath(value) != value
    ):
        _fail(path, "must be a normalized absolute path with portable segments")


def _validate_subpath(value: str, path: str) -> None:
    if value == ".":
        return
    if (
        not isinstance(value, str)
        or not PORTABLE_RELATIVE_PATH_RE.fullmatch(value)
        or posixpath.normpath(value) != value
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        _fail(path, 'must be "." or a normalized portable relative path')


def _validate_dataset(value: str, path: str) -> None:
    if not isinstance(value, str) or not DATASET_RE.fullmatch(value):
        _fail(path, "must name a dataset below a pool")


def _validate_mode(value: str, path: str) -> None:
    if not isinstance(value, str) or not FILE_MODE_RE.fullmatch(value):
        _fail(path, "must be a four-digit octal mode")


def _validate_subdirectories(values: tuple[str, ...], path: str) -> None:
    if not isinstance(values, tuple):
        _fail(f"{path}.subdirectories", "must be a tuple")
    for subdirectory in values:
        _validate_subpath(subdirectory, f"{path}.subdirectories")
        if subdirectory == ".":
            _fail(f"{path}.subdirectories", 'cannot contain "."')
    if len(set(values)) != len(values):
        _fail(f"{path}.subdirectories", "cannot contain duplicates")
