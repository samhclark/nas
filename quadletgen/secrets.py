"""Validate that declared runtime secrets exist in the encrypted SOPS file."""

from __future__ import annotations

import re
from pathlib import Path

from .model import ConfigError, Fleet


def verify_sops(fleet: Fleet, sops_file: Path) -> None:
    declared = {
        (service.source.name, secret.name)
        for service in fleet.services
        for secret in service.container.secrets
    }
    if fleet.egress is not None:
        declared.add(("_fleet.toml", fleet.egress.secret_name))
    declared.update(
        ("_fleet.toml", secret)
        for consumer in fleet.host_secret_consumers
        for secret in consumer.secrets
    )
    if not declared:
        return
    if not sops_file.exists():
        raise ConfigError(f"secrets are declared but {sops_file} does not exist")
    available = _sops_secret_keys(sops_file)
    missing = [
        (toml, secret)
        for toml, secret in sorted(declared)
        if secret not in available
    ]
    if missing:
        details = "; ".join(
            f"{toml}: secret {secret!r} not found in {sops_file.name}"
            for toml, secret in missing
        )
        raise ConfigError(details)


def _sops_secret_keys(sops_file: Path) -> set[str]:
    # SOPS leaves top-level YAML key names in the clear; the only structural
    # key it adds is its own "sops" metadata block.
    keys = set()
    for line in sops_file.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+):", line)
        if match:
            keys.add(match.group(1))
    keys.discard("sops")
    return keys
