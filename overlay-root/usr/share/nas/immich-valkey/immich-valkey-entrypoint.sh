#!/usr/bin/env bash
# ABOUTME: Applies the Valkey microVM's private overcommit policy before startup.
# ABOUTME: Normalizes libkrun's guest-root payload to the declared 1000:1000 user.

set -euo pipefail

overcommit_path="${NAS_VALKEY_OVERCOMMIT_PATH:-/proc/sys/vm/overcommit_memory}"
server="${NAS_VALKEY_SERVER:-/usr/local/bin/valkey-server}"
effective_uid="$(id -u)"
effective_gid="$(id -g)"

case "${effective_uid}:${effective_gid}" in
    0:0|1000:1000)
        ;;
    *)
        printf '%s\n' \
            "immich-valkey entrypoint: unsupported identity ${effective_uid}:${effective_gid}; " \
            "expected 0:0 or 1000:1000" >&2
        exit 1
        ;;
esac

if [[ "$(<"${overcommit_path}")" != 1 ]]; then
    if [[ "${effective_uid}" != 0 ]]; then
        printf '%s\n' \
            "immich-valkey entrypoint: ${overcommit_path} is not 1 and guest root is required to change it" >&2
        exit 1
    fi
    printf '1\n' >"${overcommit_path}"
fi

if [[ "$(<"${overcommit_path}")" != 1 ]]; then
    printf '%s\n' \
        "immich-valkey entrypoint: failed to set ${overcommit_path} to 1" >&2
    exit 1
fi

if [[ "${effective_uid}" == 0 ]]; then
    printf '%s\n' \
        "immich-valkey entrypoint: enabled guest vm.overcommit_memory; dropping to 1000:1000 via setpriv" >&2
    exec setpriv --reuid=1000 --regid=1000 --clear-groups -- "${server}" "$@"
fi

printf '%s\n' \
    "immich-valkey entrypoint: guest vm.overcommit_memory is enabled; using existing identity 1000:1000" >&2
exec "${server}" "$@"
