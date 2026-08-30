#!/bin/bash
# ABOUTME: Provides hardened libkrun launching and atomic backup metric helpers.

set -euo pipefail

nas_backup_resolve_host_tap() {
    local tap_id=${NAS_BACKUP_HOST_TAP_ID:-immich-backup}
    local manifest=${NAS_BACKUP_HOST_TAP_MANIFEST:-/usr/share/nas/fleet/host-vm-taps.tsv}
    local -a matches=()

    if [[ ! -r "${manifest}" ]]; then
        printf 'Host-VM TAP manifest is unavailable: %s\n' "${manifest}" >&2
        return 1
    fi
    mapfile -t matches < <(
        /usr/bin/awk -F '\t' -v tap_id="${tap_id}" '$1 == tap_id { print $2 }' "${manifest}"
    )
    if (( ${#matches[@]} != 1 )); then
        printf 'Host-VM TAP %s must have exactly one manifest entry.\n' "${tap_id}" >&2
        return 1
    fi
    if [[ ! "${matches[0]}" =~ ^krun-[a-zA-Z0-9_-]+$ ]] || (( ${#matches[0]} > 15 )); then
        printf 'Host-VM TAP %s has an invalid interface: %s\n' "${tap_id}" "${matches[0]}" >&2
        return 1
    fi
    printf '%s\n' "${matches[0]}"
}

nas_backup_require_outbound_network() {
    local tap_name=$1
    local tun_device=${NAS_BACKUP_TUN_DEVICE:-/dev/net/tun}
    local ready_file=${NAS_BACKUP_NETWORK_READY_FILE:-/run/nas-krun-network/policy-ready}
    local boot_id_file=${NAS_BACKUP_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}
    local sys_class_net=${NAS_BACKUP_SYS_CLASS_NET:-/sys/class/net}
    local current_boot ready_boot

    if [[ ! -c "${tun_device}" ]]; then
        printf 'Immich backup TAP device is unavailable: %s\n' "${tun_device}" >&2
        return 1
    fi
    if [[ ! -e "${sys_class_net}/${tap_name}" ]]; then
        printf 'Immich backup TAP interface is unavailable: %s\n' "${tap_name}" >&2
        return 1
    fi
    if [[ ! -r "${ready_file}" || ! -r "${boot_id_file}" ]]; then
        printf 'libkrun network policy readiness is unavailable.\n' >&2
        return 1
    fi
    current_boot="$(<"${boot_id_file}")"
    ready_boot="$(<"${ready_file}")"
    if [[ -z "${current_boot}" || "${ready_boot}" != "${current_boot}" ]]; then
        printf 'libkrun network policy readiness is stale.\n' >&2
        return 1
    fi
}

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
    local network_lock_fd=
    local tap_id=${NAS_BACKUP_HOST_TAP_ID:-immich-backup}
    local tap_name=

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

    case "${network_mode}" in
        none) ;;
        outbound)
            tap_name="$(nas_backup_resolve_host_tap)" || return
            exec {network_lock_fd}<>"${NAS_BACKUP_NETWORK_LOCK_FILE:-/run/lock/nas-host-vm-tap-network.lock}" || {
                printf 'Could not open the host-VM TAP network lease.\n' >&2
                return 1
            }
            if ! /usr/bin/flock --shared "${network_lock_fd}"; then
                exec {network_lock_fd}>&-
                return 1
            fi
            if ! nas_backup_require_outbound_network "${tap_name}"; then
                /usr/bin/flock --unlock "${network_lock_fd}"
                exec {network_lock_fd}>&-
                return 1
            fi
            network_arguments=(
                --network=host
                --device="${NAS_BACKUP_TUN_DEVICE:-/dev/net/tun}"
                --annotation="krun.tap_name=${tap_name}"
                --label="io.samhclark.nas.host-vm-tap=${tap_id}"
            )
            ;;
        *)
            printf 'Unsupported backup VM network mode: %s\n' "${network_mode}" >&2
            return 2
            ;;
    esac

    local status=0
    "${podman_bin}" run --rm --pull=missing --runtime=krun \
        --annotation=krun.cpus=2 --annotation=krun.ram_mib=2048 \
        "${network_arguments[@]}" \
        --read-only --cap-drop=all --security-opt=no-new-privileges \
        --pids-limit=512 --cpu-shares=128 --blkio-weight=100 \
        --tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m \
        --env=HOME=/tmp --env=XDG_CACHE_HOME=/tmp/cache \
        "${environment_arguments[@]}" \
        "${podman_arguments[@]}" \
        "${image}" "${guest_arguments[@]}" || status=$?
    if [[ -n "${network_lock_fd}" ]]; then
        /usr/bin/flock --unlock "${network_lock_fd}"
        exec {network_lock_fd}>&-
    fi
    return "${status}"
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
