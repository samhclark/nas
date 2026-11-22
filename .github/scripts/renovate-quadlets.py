"""Prepare generated file additions for a Renovate PR from a clean merge base."""

import base64
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def prepare(head: str, output: Path) -> None:
    # Never check out the PR. Its only accepted input is a new pin for an
    # existing image in an otherwise byte-identical regular service TOML.
    changes = git("diff", "--raw", "--no-renames", "-z", "HEAD", head)
    entries = changes.split(b"\0")[:-1]
    sources = {}
    units = set()
    for change, path in zip(entries[::2], entries[1::2], strict=True):
        old_mode, new_mode, _, _, status = change.split()
        if (
            not re.fullmatch(rb"quadlets/[a-z0-9][a-z0-9-]*\.toml", path)
            or (old_mode, new_mode, status) != (b":100644", b"100644", b"M")
        ):
            print("Skipping PR with changes beyond regular Quadlet sources.")
            return
        source = Path(path.decode())
        original = source.read_bytes()
        proposed = git("show", f"{head}:{source}")
        service = tomllib.loads(original.decode())
        old_pin = service["container"]["image"]
        new_pin = tomllib.loads(proposed.decode())["container"]["image"]
        repository = old_pin.split("@")[0].rsplit(":", 1)[0]
        pin_pattern = re.escape(repository) + r":\w[\w.-]{0,127}@sha256:[0-9a-f]{64}"
        old_literal = f'"{old_pin}"'.encode()
        if (
            not isinstance(new_pin, str)
            or not re.fullmatch(pin_pattern, new_pin, re.ASCII)
            or original.count(old_literal) != 1
            or original.replace(old_literal, f'"{new_pin}"'.encode()) != proposed
        ):
            print(f"Skipping {source}: expected only a tag or digest change.")
            return
        sources[source] = proposed
        uid = service["host"]["uid"]
        name = service["service"]["name"]
        units.add(f"overlay-root/etc/containers/systemd/users/{uid}/{name}.container")

    if not sources:
        return
    for source, proposed in sources.items():
        source.write_bytes(proposed)
    subprocess.run(["make", "generate-quadlets"], check=True)

    additions = []
    changed_outputs = git("diff", "--name-only", "-z", "--", "overlay-root")
    for path in changed_outputs.decode().split("\0")[:-1]:
        if path not in units:
            raise ValueError(f"generator changed unexpected file: {path}")
        contents = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        additions.append({"path": path, "contents": contents})
    if additions:
        output.write_text(json.dumps(additions))


if __name__ == "__main__":
    prepare(sys.argv[1], Path(sys.argv[2]))
