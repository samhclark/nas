#!/usr/bin/env python3
# ABOUTME: Probes database identity and Valkey's explicit guest-root bootstrap.
# ABOUTME: Retains the host-side OCI user and keep-id mapping in both probes.

"""Classify the effective identity observed inside the krun database guest."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from quadletgen.parser import load_service  # noqa: E402


CONTAINER_CLI = os.environ.get("CONTAINER_CLI", "podman")
COMMAND_TIMEOUT_SECONDS = 60
DATABASE_SPEC = REPO / "quadlets" / "immich-database.toml"
VALKEY_SPEC = REPO / "quadlets" / "immich-valkey.toml"
IDENTITY_PATTERN = re.compile(
    r"\buid=(?P<uid>\d+)(?:\([^)]*\))?\s+"
    r"gid=(?P<gid>\d+)(?:\([^)]*\))?"
)


def probe_command(
    image: str,
    container_cli: str = CONTAINER_CLI,
    *,
    guest_bootstrap_root: bool = False,
) -> list[str]:
    command = [
        container_cli,
        "run",
        "--rm",
        "--pull=missing",
        "--runtime=krun",
        "--network=none",
        "--user=1000:1000",
        "--userns=keep-id:uid=1000,gid=1000",
        "--entrypoint=/usr/bin/id",
    ]
    if guest_bootstrap_root:
        command.append("--annotation=krun.guest_bootstrap_root=1")
    return [*command, image]


def classify_identity(output: str) -> str:
    match = IDENTITY_PATTERN.search(output)
    if match is None:
        compact = ":".join(output.split())
        if compact in {"0:0", "1000:1000"}:
            uid, gid = (int(part) for part in compact.split(":", 1))
        else:
            raise ValueError(f"could not parse krun identity: {output.strip()!r}")
    else:
        uid = int(match.group("uid"))
        gid = int(match.group("gid"))

    if (uid, gid) == (0, 0):
        return "guest-root-fallback"
    if (uid, gid) == (1000, 1000):
        return "honored-1000:1000"
    raise ValueError(f"unexpected krun identity: {uid}:{gid}")


def probe_identity(image: str, *, guest_bootstrap_root: bool = False) -> str:
    command = probe_command(image, guest_bootstrap_root=guest_bootstrap_root)
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"krun identity probe failed with status {result.returncode}: {detail}"
        )

    return classify_identity(result.stdout)


def main() -> int:
    database = load_service(DATABASE_SPEC)
    classification = probe_identity(database.container.image)
    print(f"krun database identity: {classification}", flush=True)
    valkey = load_service(VALKEY_SPEC)
    if not valkey.tap_spec.guest_bootstrap_root:
        raise RuntimeError("Valkey requires explicit guest-root bootstrap")
    classification = probe_identity(valkey.container.image, guest_bootstrap_root=True)
    if classification != "guest-root-fallback":
        raise RuntimeError(
            "krun did not supply Valkey's requested guest-root bootstrap: "
            f"{classification}"
        )
    print("krun Valkey bootstrap identity: 0:0 (host OCI user remains 1000:1000)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
