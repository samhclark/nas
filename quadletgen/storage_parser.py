# ABOUTME: Strictly decodes [[storage]] tables into the typed storage model.
# ABOUTME: Rejects implicit lifecycle behavior and unrecognized ZFS properties.

"""Strict parser for service-local ``[[storage]]`` declarations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import NoReturn

from .errors import ConfigError
from .storage_model import (
    DirectoryStorage,
    FleetZfsStorage,
    ManagedZfsStorage,
    SharedStorageExport,
    StorageAccess,
    StorageExport,
    StorageSpec,
    ZfsCompression,
    ZfsPrimaryCache,
    ZfsRecordSize,
)


COMMON_KEYS = {"name", "kind", "host-path", "exports"}
DIRECTORY_KEYS = COMMON_KEYS | {"mode", "subdirectories"}
MANAGED_ZFS_KEYS = COMMON_KEYS | {
    "dataset",
    "mode",
    "record-size",
    "compression",
    "atime",
    "primary-cache",
}


def parse_storage(raw: object, source: str) -> tuple[StorageSpec, ...]:
    """Parse the value represented by a service's ``[[storage]]`` tables."""

    path = f"{source}: [[storage]]"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _fail(path, "must be an array of tables")

    storage = tuple(
        _parse_storage_item(item, source, index)
        for index, item in enumerate(raw, start=1)
    )
    _validate_collection(storage, path)
    return storage


def parse_shared_storage(
    raw: object,
    source: str,
) -> tuple[SharedStorageExport, ...]:
    """Decode service references to fleet-owned storage resources."""

    path = f"{source}: [[shared-storage]]"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _fail(path, "must be an array of tables")
    result = []
    for index, item in enumerate(raw, start=1):
        item_path = f"{path}[{index}]"
        table = _table(
            item,
            item_path,
            {"resource", "subpath", "container-path", "access"},
        )
        access = _string(
            _required(table, "access", item_path),
            f"{item_path}.access",
        )
        if access not in {item.value for item in StorageAccess}:
            _fail(
                f"{item_path}.access",
                'must be "read-only" or "read-write"',
            )
        try:
            result.append(
                SharedStorageExport(
                    resource=_string(
                        _required(table, "resource", item_path),
                        f"{item_path}.resource",
                    ),
                    subpath=_string(
                        _required(table, "subpath", item_path),
                        f"{item_path}.subpath",
                    ),
                    container_path=_string(
                        _required(table, "container-path", item_path),
                        f"{item_path}.container-path",
                    ),
                    access=StorageAccess(access),
                )
            )
        except ConfigError as error:
            _reraise_model_error(error, source)
    targets = [export.container_path for export in result]
    if len(set(targets)) != len(targets):
        _fail(path, "cannot repeat a container path")
    return tuple(result)


def _parse_storage_item(raw: object, source: str, index: int) -> StorageSpec:
    item_path = f"{source}: [[storage]][{index}]"
    discriminator = _table(raw, item_path, COMMON_KEYS | {
        "mode",
        "subdirectories",
        "dataset",
        "record-size",
        "compression",
        "atime",
        "primary-cache",
    })
    kind = _string(
        _required(discriminator, "kind", item_path),
        f"{item_path}.kind",
    )
    if kind == "directory":
        table = _table(raw, item_path, DIRECTORY_KEYS)
        return _construct_directory(table, source, item_path)
    if kind == "managed-zfs":
        table = _table(raw, item_path, MANAGED_ZFS_KEYS)
        return _construct_managed_zfs(table, source, item_path)
    _fail(
        f"{item_path}.kind",
        'must be "directory" or "managed-zfs"',
    )


def parse_fleet_storage(raw: object, source: str) -> tuple[FleetZfsStorage, ...]:
    """Decode validation-only, required-existing fleet storage resources."""

    path = f"{source}: [[resources]]"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        _fail(path, "must be an array of tables")
    result = []
    for index, item in enumerate(raw, start=1):
        item_path = f"{path}[{index}]"
        table = _table(
            item,
            item_path,
            {
                "name",
                "kind",
                "dataset",
                "host-path",
                "shared-group",
                "mode",
                "required-paths",
            },
        )
        kind = _string(
            _required(table, "kind", item_path),
            f"{item_path}.kind",
        )
        if kind != "existing-zfs":
            _fail(f"{item_path}.kind", 'must be "existing-zfs"')
        try:
            result.append(
                FleetZfsStorage(
                    name=_string(
                        _required(table, "name", item_path),
                        f"{item_path}.name",
                    ),
                    dataset=_string(
                        _required(table, "dataset", item_path),
                        f"{item_path}.dataset",
                    ),
                    host_path=_string(
                        _required(table, "host-path", item_path),
                        f"{item_path}.host-path",
                    ),
                    shared_group=_string(
                        _required(table, "shared-group", item_path),
                        f"{item_path}.shared-group",
                    ),
                    mode=_string(
                        _required(table, "mode", item_path),
                        f"{item_path}.mode",
                    ),
                    required_paths=_string_array(
                        _required(table, "required-paths", item_path),
                        f"{item_path}.required-paths",
                    ),
                )
            )
        except ConfigError as error:
            _reraise_model_error(error, source)
    names = [item.name for item in result]
    if len(set(names)) != len(names):
        _fail(path, "contains duplicate names")
    return tuple(result)


def _construct_directory(
    table: Mapping[str, object],
    source: str,
    path: str,
) -> DirectoryStorage:
    try:
        return DirectoryStorage(
            name=_parse_name(table, path),
            host_path=_parse_host_path(table, path),
            mode=_string(_required(table, "mode", path), f"{path}.mode"),
            subdirectories=_string_array(
                table.get("subdirectories", []),
                f"{path}.subdirectories",
            ),
            exports=_parse_exports(_required(table, "exports", path), path),
        )
    except ConfigError as error:
        _reraise_model_error(error, source)


def _construct_managed_zfs(
    table: Mapping[str, object],
    source: str,
    path: str,
) -> ManagedZfsStorage:
    try:
        return ManagedZfsStorage(
            name=_parse_name(table, path),
            dataset=_string(
                _required(table, "dataset", path),
                f"{path}.dataset",
            ),
            host_path=_parse_host_path(table, path),
            mode=_string(_required(table, "mode", path), f"{path}.mode"),
            record_size=_record_size(
                _required(table, "record-size", path),
                f"{path}.record-size",
            ),
            compression=_compression(
                _required(table, "compression", path),
                f"{path}.compression",
            ),
            atime=_boolean(_required(table, "atime", path), f"{path}.atime"),
            primary_cache=_primary_cache(
                _required(table, "primary-cache", path),
                f"{path}.primary-cache",
            ),
            exports=_parse_exports(_required(table, "exports", path), path),
        )
    except ConfigError as error:
        _reraise_model_error(error, source)


def _parse_name(table: Mapping[str, object], path: str) -> str:
    return _string(_required(table, "name", path), f"{path}.name")


def _parse_host_path(table: Mapping[str, object], path: str) -> str:
    return _string(_required(table, "host-path", path), f"{path}.host-path")


def _parse_exports(raw: object, path: str) -> tuple[StorageExport, ...]:
    exports_path = f"{path}.exports"
    if not isinstance(raw, list):
        _fail(exports_path, "must be an array of tables")
    exports = []
    for index, item in enumerate(raw, start=1):
        item_path = f"{exports_path}[{index}]"
        table = _table(
            item,
            item_path,
            {"subpath", "container-path", "access"},
        )
        access_text = _string(
            _required(table, "access", item_path),
            f"{item_path}.access",
        )
        if access_text not in {item.value for item in StorageAccess}:
            _fail(
                f"{item_path}.access",
                'must be "read-only" or "read-write"',
            )
        exports.append(
            StorageExport(
                subpath=_string(
                    _required(table, "subpath", item_path),
                    f"{item_path}.subpath",
                ),
                container_path=_string(
                    _required(table, "container-path", item_path),
                    f"{item_path}.container-path",
                ),
                access=StorageAccess(access_text),
            )
        )
    return tuple(exports)


def _record_size(value: object, path: str) -> ZfsRecordSize:
    text = _string(value, path)
    if text not in {item.value for item in ZfsRecordSize}:
        _fail(path, "must be one of 4K, 16K, 32K, 128K, or 1M")
    return ZfsRecordSize(text)


def _compression(value: object, path: str) -> ZfsCompression:
    text = _string(value, path)
    if text not in {item.value for item in ZfsCompression}:
        _fail(path, 'must be "off" or "lz4"')
    return ZfsCompression(text)


def _primary_cache(value: object, path: str) -> ZfsPrimaryCache:
    text = _string(value, path)
    if text not in {item.value for item in ZfsPrimaryCache}:
        _fail(path, 'must be "all" or "metadata"')
    return ZfsPrimaryCache(text)


def _validate_collection(storage: tuple[StorageSpec, ...], path: str) -> None:
    names = [item.name for item in storage]
    if len(set(names)) != len(names):
        _fail(path, "contains duplicate names")
    host_paths = [item.host_path for item in storage]
    if len(set(host_paths)) != len(host_paths):
        _fail(path, "contains duplicate host paths")
    targets = [
        export.container_path
        for item in storage
        for export in item.exports
    ]
    if len(set(targets)) != len(targets):
        _fail(path, "contains duplicate container paths")
    datasets = [
        item.dataset
        for item in storage
        if isinstance(item, ManagedZfsStorage)
    ]
    if len(set(datasets)) != len(datasets):
        _fail(path, "contains duplicate ZFS datasets")


def _table(
    value: object,
    path: str,
    allowed: set[str],
) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(path, "must be a table")
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail(path, f"has unknown keys: {', '.join(unknown)}")
    return value


def _required(table: Mapping[str, object], key: str, path: str) -> object:
    if key not in table:
        _fail(path, f"is missing {key!r}")
    return table[key]


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(path, "must be a non-empty string")
    if any(not character.isprintable() for character in value):
        _fail(path, "cannot contain control characters")
    return value


def _string_array(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(path, "must be an array of strings")
    return tuple(
        _string(item, f"{path}[{index}]")
        for index, item in enumerate(value, start=1)
    )


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        _fail(path, "must be a boolean")
    return value


def _reraise_model_error(error: ConfigError, source: str) -> NoReturn:
    message = str(error)
    if message.startswith(f"{source}:"):
        raise error
    raise ConfigError(f"{source}: {message}") from error


def _fail(path: str, message: str) -> NoReturn:
    raise ConfigError(f"{path}: {message}")
