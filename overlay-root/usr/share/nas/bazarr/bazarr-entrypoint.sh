#!/usr/bin/env bash
# ABOUTME: Starts Bazarr under the NAS libkrun mapped media identity.

set -euo pipefail

# Sidecar subtitles must stay writable by the shared media group.
umask 002

effective_uid="$(id -u)"
effective_gid="$(id -g)"
if [[ "${effective_uid}" == 0 ]]; then
    exec s6-setuidgid 1000:1000 "$0" "$@"
fi
if [[ "${effective_uid}:${effective_gid}" != 1000:1000 ]]; then
    printf 'bazarr entrypoint: unsupported effective identity %s:%s; expected 1000:1000\n' \
        "${effective_uid}" "${effective_gid}" >&2
    exit 1
fi

# Use the image's virtualenv directly because this bypasses its s6 init.
cd /app/bazarr/bin
exec /lsiopy/bin/python3 /app/bazarr/bin/bazarr.py --no-update --config /config "$@"
