"""Command-line entry point for the rootless service fleet compiler."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .compiler import compile_fleet
from .model import ConfigError, Fleet
from .parser import (
    load_fleet_config,
    load_fleet_egress,
    load_fleet_storage,
    load_host_secret_consumers,
    load_service,
)
from .secrets import verify_sops
from .sync import check_artifacts, sync_artifacts


def run(repo: Path, *, check: bool = False) -> int:
    quadlet_dir = repo / "quadlets"
    overlay = repo / "overlay-root"
    fleet_config_path = quadlet_dir / "_fleet.toml"
    toml_paths = sorted(quadlet_dir.glob("[!_]*.toml"))
    if not toml_paths:
        raise ConfigError(f"no TOML configs found in {quadlet_dir}")
    groups = (
        load_fleet_config(fleet_config_path)
        if fleet_config_path.exists()
        else ()
    )
    resources = (
        load_fleet_storage(fleet_config_path)
        if fleet_config_path.exists()
        else ()
    )
    fleet = Fleet.build(
        [load_service(path) for path in toml_paths],
        groups=groups,
        resources=resources,
        egress=(
            load_fleet_egress(fleet_config_path)
            if fleet_config_path.exists()
            else None
        ),
        host_secret_consumers=(
            load_host_secret_consumers(fleet_config_path)
            if fleet_config_path.exists()
            else ()
        ),
    )
    verify_sops(
        fleet,
        overlay / "usr/share/nas/secrets/secrets.sops.yaml",
    )
    for service in fleet.services:
        if not service.container.enabled:
            print(f"skip  quadlets/{service.source.name} container (disabled)")
    artifacts = compile_fleet(fleet)
    if check:
        check_artifacts(repo, overlay, artifacts)
    else:
        sync_artifacts(repo, overlay, artifacts)
    return 0


def main(repo: Path, argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile the rootless service fleet into the image overlay."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify generated output without modifying it",
    )
    arguments = parser.parse_args(argv)
    try:
        return run(repo, check=arguments.check)
    except ConfigError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
