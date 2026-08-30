#!/bin/bash
# ABOUTME: Provides hardened libkrun launching and atomic backup metric helpers.

set -euo pipefail

nas_backup_run_vm() {
    local network_mode=$1
    local image=$2
    local environment_file=$3
    shift 3

    local podman_bin="${PODMAN_BIN:-/usr/bin/podman}"
    local -a network_arguments=(--network=none)
    local -a environment_arguments=()
    local -a podman_arguments=()
    local -a guest_arguments=()
    local guest_command=false

    case "${network_mode}" in
        none) ;;
        outbound)
            # libkrun's OCI handler creates the guest's outbound virtio-net
            # device when passt is explicitly selected.
            network_arguments=(--annotation=krun.use_passt=1)
            ;;
        *)
            printf 'Unsupported backup VM network mode: %s\n' "${network_mode}" >&2
            return 2
            ;;
    esac

    if [[ "${environment_file}" != - ]]; then
        environment_arguments=(--env-file="${environment_file}")
    fi

    while (( $# > 0 )); do
        if [[ "$1" == -- ]] && [[ "${guest_command}" == false ]]; then
            guest_command=true
        elif [[ "${guest_command}" == false ]]; then
            podman_arguments+=("$1")
        else
            guest_arguments+=("$1")
        fi
        shift
    done
    if [[ "${guest_command}" == false ]]; then
        printf 'Backup VM invocation is missing its command separator.\n' >&2
        return 2
    fi

    "${podman_bin}" run --rm --pull=missing --runtime=krun \
        --annotation=krun.cpus=2 --annotation=krun.ram_mib=2048 \
        "${network_arguments[@]}" \
        --read-only --cap-drop=all --security-opt=no-new-privileges \
        --pids-limit=512 --cpu-shares=128 --blkio-weight=100 \
        --tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m \
        --env=HOME=/tmp --env=XDG_CACHE_HOME=/tmp/cache \
        "${environment_arguments[@]}" \
        "${podman_arguments[@]}" \
        "${image}" "${guest_arguments[@]}"
}

nas_backup_write_metrics() {
    local destination=$1
    local temporary="${destination}.$$"

    /usr/bin/mkdir -p -- "${destination%/*}"
    /usr/bin/cat > "${temporary}"
    /usr/bin/chmod 0644 "${temporary}"
    /usr/bin/mv -f -- "${temporary}" "${destination}"
}

nas_backup_read_timestamp() {
    local path=$1
    if [[ -s "${path}" ]]; then
        /usr/bin/cat -- "${path}"
    else
        printf '0\n'
    fi
}

nas_backup_record_timestamp() {
    local path=$1
    local value=$2
    local temporary="${path}.$$"

    /usr/bin/mkdir -p -- "${path%/*}"
    printf '%s\n' "${value}" > "${temporary}"
    /usr/bin/chmod 0600 "${temporary}"
    /usr/bin/mv -f -- "${temporary}" "${path}"
}
