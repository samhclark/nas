#!/bin/bash
# ABOUTME: Applies a generated, declarative storage manifest without ever
# destroying datasets or changing properties on datasets that already exist.

set -euo pipefail

readonly MANIFEST_VERSION="nas-storage-manifest-v3"
readonly EXPECTED_LABEL="system_u:object_r:container_file_t:s0"
readonly EXPECTED_LABEL_SUFFIX="object_r:container_file_t:s0"
readonly REPAIR_REQUIRED_STATUS=78

declare SERVICE_NAME=""
declare SERVICE_USER=""
declare SERVICE_UID=""
declare SERVICE_GID=""
declare SERVICE_SUBID_START=""
declare SERVICE_SUBID_COUNT=""
declare SERVICE_CONTAINER=""
declare REPAIR_REQUEST=""
declare HAS_ZFS=0
declare MANIFEST_SERVICE=""
declare -a SERVICE_PORTS=()
declare -a SERVICE_SUPPLEMENTAL_GIDS=()

declare -a ENTRY_KIND=()
declare -a ENTRY_DATASET=()
declare -a ENTRY_PATH=()
declare -a ENTRY_MODE=()
declare -a ENTRY_PROPERTIES=()
declare -a ENTRY_WAS_MISSING=()
declare -A SEEN_DATASETS=()
declare -A SEEN_PATHS=()

die() {
    printf 'nas-prepare-storage: ERROR: %s\n' "$*" >&2
    exit 1
}

repair_required() {
    printf 'nas-prepare-storage: ERROR: %s\n' "$*" >&2
    exit "${REPAIR_REQUIRED_STATUS}"
}

log() {
    printf 'nas-prepare-storage: %s\n' "$*"
}

usage() {
    printf 'Usage: %s /absolute/path/to/service.storage-manifest\n' "${0##*/}" >&2
    exit 2
}

is_absolute_path() {
    local path="$1"

    [[ "${path}" =~ ^/[A-Za-z0-9._/-]+$ ]] &&
        [[ "${path}" != "/" ]] &&
        [[ "${path}" != */ ]] &&
        [[ "${path}" != *//* ]] &&
        [[ "/${path#/}/" != *"/../"* ]] &&
        [[ "/${path#/}/" != *"/./"* ]]
}

validate_mode() {
    [[ "$1" =~ ^0[0-7]{3}$ ]]
}

validate_dataset() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]*(/[A-Za-z0-9][A-Za-z0-9_.:-]*)+$ ]]
}

validate_properties() {
    local properties="$1"
    local property
    local key
    local value
    local -A seen=()
    local -a requested=()

    [[ "${properties}" == "-" ]] && return
    IFS=',' read -r -a requested <<< "${properties}"
    ((${#requested[@]} > 0)) || return 1

    for property in "${requested[@]}"; do
        [[ "${property}" == *=* ]] || return 1
        key="${property%%=*}"
        value="${property#*=}"
        [[ -n "${key}" && -n "${value}" && -z "${seen[${key}]+x}" ]] || return 1
        seen["${key}"]=1

        case "${key}" in
            recordsize)
                [[ "${value}" =~ ^(4K|16K|32K|128K|1M)$ ]] || return 1
                ;;
            compression)
                [[ "${value}" == "off" || "${value}" == "lz4" ]] || return 1
                ;;
            atime)
                [[ "${value}" == "on" || "${value}" == "off" ]] || return 1
                ;;
            primarycache)
                [[ "${value}" == "all" || "${value}" == "metadata" ]] || return 1
                ;;
            *)
                return 1
                ;;
        esac
    done
}

register_path() {
    local path="$1"
    local line_number="$2"

    [[ -z "${SEEN_PATHS[${path}]+x}" ]] || die "line ${line_number}: duplicate storage path '${path}'"
    SEEN_PATHS["${path}"]=1
}

register_dataset() {
    local dataset="$1"
    local line_number="$2"

    [[ -z "${SEEN_DATASETS[${dataset}]+x}" ]] || die "line ${line_number}: duplicate dataset '${dataset}'"
    SEEN_DATASETS["${dataset}"]=1
}

append_entry() {
    ENTRY_KIND+=("$1")
    ENTRY_DATASET+=("$2")
    ENTRY_PATH+=("$3")
    ENTRY_MODE+=("$4")
    ENTRY_PROPERTIES+=("$5")
    ENTRY_WAS_MISSING+=(0)
}

parse_service_record() {
    local line_number="$1"
    shift
    local ports
    local port
    local supplemental_gids
    local supplemental_gid
    local -A seen_ports=()
    local -A seen_supplemental_gids=()
    local -a requested_ports=()
    local -a requested_supplemental_gids=()

    (($# == 8)) || die "line ${line_number}: service records require 8 fields"
    [[ -z "${SERVICE_NAME}" ]] || die "line ${line_number}: duplicate service record"

    SERVICE_NAME="$1"
    SERVICE_USER="$2"
    SERVICE_UID="$3"
    SERVICE_GID="$4"
    SERVICE_SUBID_START="$5"
    SERVICE_SUBID_COUNT="$6"
    SERVICE_CONTAINER="${SERVICE_NAME}"
    ports="$7"
    supplemental_gids="$8"

    [[ "${SERVICE_NAME}" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || die "line ${line_number}: invalid service name"
    [[ "${SERVICE_USER}" =~ ^_nas_[a-z][a-z0-9]*$ ]] || die "line ${line_number}: invalid service user"
    [[ "${SERVICE_UID}" =~ ^51[0-9]{3}$ ]] || die "line ${line_number}: service UID is outside 51000-51999"
    [[ "${SERVICE_GID}" == "${SERVICE_UID}" ]] || die "line ${line_number}: service UID and GID must match"
    [[ "${SERVICE_SUBID_START}" =~ ^[1-9][0-9]{8}$ ]] || die "line ${line_number}: invalid subordinate ID start"
    [[ "${SERVICE_SUBID_COUNT}" == "65536" ]] || die "line ${line_number}: subordinate ID count must be 65536"
    ((10#${SERVICE_SUBID_START} + 10#${SERVICE_SUBID_COUNT} <= 4294967296)) ||
        die "line ${line_number}: subordinate ID range exceeds uint32"

    if [[ "${ports}" != "-" ]]; then
        IFS=',' read -r -a requested_ports <<< "${ports}"
        for port in "${requested_ports[@]}"; do
            [[ "${port}" =~ ^[1-9][0-9]{0,4}$ ]] || die "line ${line_number}: invalid TCP port '${port}'"
            ((10#${port} <= 65535)) || die "line ${line_number}: invalid TCP port '${port}'"
            [[ -z "${seen_ports[${port}]+x}" ]] || die "line ${line_number}: duplicate TCP port '${port}'"
            seen_ports["${port}"]=1
            SERVICE_PORTS+=("${port}")
        done
    fi

    if [[ "${supplemental_gids}" != "-" ]]; then
        IFS=',' read -r -a requested_supplemental_gids <<< "${supplemental_gids}"
        for supplemental_gid in "${requested_supplemental_gids[@]}"; do
            [[ "${supplemental_gid}" =~ ^[1-9][0-9]{0,9}$ ]] ||
                die "line ${line_number}: invalid supplemental GID '${supplemental_gid}'"
            ((10#${supplemental_gid} <= 4294967295)) ||
                die "line ${line_number}: invalid supplemental GID '${supplemental_gid}'"
            [[ "${supplemental_gid}" != "${SERVICE_GID}" ]] ||
                die "line ${line_number}: supplemental GID repeats the service GID"
            [[ -z "${seen_supplemental_gids[${supplemental_gid}]+x}" ]] ||
                die "line ${line_number}: duplicate supplemental GID '${supplemental_gid}'"
            seen_supplemental_gids["${supplemental_gid}"]=1
            SERVICE_SUPPLEMENTAL_GIDS+=("${supplemental_gid}")
        done
    fi
}

parse_storage_record() {
    local line_number="$1"
    local kind="$2"
    shift 2
    local dataset
    local mode
    local path
    local properties

    [[ -n "${SERVICE_NAME}" ]] || die "line ${line_number}: the service record must precede storage records"

    case "${kind}" in
        directory)
            (($# == 2)) || die "line ${line_number}: directory records require 2 fields"
            path="$1"
            mode="$2"
            is_absolute_path "${path}" || die "line ${line_number}: invalid directory path"
            [[ "${path}" == /var/* ]] || die "line ${line_number}: directory paths must be below /var"
            validate_mode "${mode}" || die "line ${line_number}: invalid directory mode"
            register_path "${path}" "${line_number}"
            append_entry "directory" "-" "${path}" "${mode}" "-"
            ;;
        managed-zfs)
            (($# == 4)) || die "line ${line_number}: managed-zfs records require 4 fields"
            dataset="$1"
            path="$2"
            mode="$3"
            properties="$4"
            validate_dataset "${dataset}" || die "line ${line_number}: invalid managed dataset"
            [[ "${dataset}" == "tank/${SERVICE_NAME}" || "${dataset}" == "tank/${SERVICE_NAME}/"* ]] ||
                die "line ${line_number}: managed datasets must be within tank/${SERVICE_NAME}"
            validate_properties "${properties}" || die "line ${line_number}: invalid or unsupported ZFS properties"
            HAS_ZFS=1
            register_dataset "${dataset}" "${line_number}"
            if [[ "${path}" == "none" ]]; then
                [[ "${mode}" == "-" ]] || die "line ${line_number}: unmounted datasets require mode '-'"
            else
                is_absolute_path "${path}" || die "line ${line_number}: invalid managed mount path"
                [[ "${path}" == /var/* ]] || die "line ${line_number}: managed mount paths must be below /var"
                validate_mode "${mode}" || die "line ${line_number}: invalid managed mount mode"
                register_path "${path}" "${line_number}"
            fi
            append_entry "managed-zfs" "${dataset}" "${path}" "${mode}" "${properties}"
            ;;
        *)
            die "line ${line_number}: unknown record type '${kind}'"
            ;;
    esac
}

parse_manifest() {
    local manifest="$1"
    local header_prefix
    local header_suffix
    local line=""
    local line_number=0
    local -a fields=()

    while IFS= read -r line || [[ -n "${line}" ]]; do
        ((line_number += 1))
        [[ -n "${line}" ]] || die "line ${line_number}: blank lines are not permitted"
        [[ "${line}" != *[$'\r\t']* ]] || die "line ${line_number}: tabs and carriage returns are not permitted"

        if ((line_number == 1)); then
            [[ "${line}" == "${MANIFEST_VERSION}" ]] || die "line 1: expected ${MANIFEST_VERSION}"
            continue
        fi
        if ((line_number == 2)); then
            header_prefix="# GENERATED by generate-quadlets.py from quadlets/"
            header_suffix=".toml — DO NOT EDIT"
            [[ "${line}" == "${header_prefix}"*"${header_suffix}" ]] ||
                die "line 2: expected the generated manifest header"
            MANIFEST_SERVICE="${line#"${header_prefix}"}"
            MANIFEST_SERVICE="${MANIFEST_SERVICE%"${header_suffix}"}"
            [[ "${MANIFEST_SERVICE}" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] ||
                die "line 2: invalid generated manifest source"
            continue
        fi

        IFS='|' read -r -a fields <<< "${line}"
        ((${#fields[@]} > 1)) || die "line ${line_number}: malformed record"
        case "${fields[0]}" in
            service)
                parse_service_record "${line_number}" "${fields[@]:1}"
                ;;
            *)
                parse_storage_record "${line_number}" "${fields[@]}"
                ;;
        esac
    done < "${manifest}"

    [[ -n "${SERVICE_NAME}" ]] || die "manifest has no service record"
    [[ "${SERVICE_NAME}" == "${MANIFEST_SERVICE}" ]] || die "generated manifest source does not match its service record"
    ((${#ENTRY_KIND[@]} > 0)) || die "manifest has no storage records"
}

require_commands() {
    local command_name

    for command_name in chown chmod find getent id install matchpathcon podman restorecon runuser semanage ss stat; do
        command -v "${command_name}" >/dev/null || die "required command '${command_name}' is unavailable"
    done
    if [[ "${HAS_ZFS}" -eq 1 ]]; then
        for command_name in findmnt zfs zpool; do
            command -v "${command_name}" >/dev/null || die "required command '${command_name}' is unavailable"
        done
    fi
}

load_zfs_inventory() {
    local output
    local name

    if ! output="$(zpool list -H -o name)"; then
        die "unable to inventory ZFS pools"
    fi
    while IFS= read -r name; do
        [[ -n "${name}" ]] && AVAILABLE_POOLS["${name}"]=1
    done <<< "${output}"

    if ! output="$(zfs list -H -o name)"; then
        die "unable to inventory ZFS datasets"
    fi
    while IFS= read -r name; do
        [[ -n "${name}" ]] && AVAILABLE_DATASETS["${name}"]=1
    done <<< "${output}"
    return 0
}

dataset_pool() {
    printf '%s\n' "${1%%/*}"
}

dataset_exists() {
    [[ -n "${AVAILABLE_DATASETS[$1]+x}" ]]
}

validate_mount() {
    local dataset="$1"
    local path="$2"
    local source

    if ! source="$(findmnt -rn -o SOURCE -T "${path}")"; then
        die "${path} is not mounted from ${dataset}"
    fi
    [[ "${source}" == "${dataset}" ]] || die "${path} is mounted from '${source}', expected '${dataset}'"
}

property_value_is_expected() {
    local expected="$1"
    local actual="$2"

    [[ "${actual}" == "${expected}" ]]
}

verify_dataset_properties() {
    local index="$1"
    local dataset="${ENTRY_DATASET[index]}"
    local path="${ENTRY_PATH[index]}"
    local properties="${ENTRY_PROPERTIES[index]}"
    local key
    local line
    local output
    local property
    local value
    local -a requested=()
    local -a keys=(mountpoint)
    local -A expected=()
    local -A actual=()

    if [[ "${path}" == "none" ]]; then
        expected[mountpoint]="none"
    else
        expected[mountpoint]="${path}"
    fi

    if [[ "${properties}" != "-" ]]; then
        IFS=',' read -r -a requested <<< "${properties}"
        for property in "${requested[@]}"; do
            key="${property%%=*}"
            expected["${key}"]="${property#*=}"
            keys+=("${key}")
        done
    fi

    local key_list
    key_list="$(IFS=,; printf '%s' "${keys[*]}")"
    if ! output="$(zfs get -H -o property,value "${key_list}" "${dataset}")"; then
        die "unable to read properties for ${dataset}"
    fi
    while IFS=$'\t' read -r property value; do
        [[ -n "${property}" && -n "${value}" ]] || die "malformed property output for ${dataset}"
        actual["${property}"]="${value}"
    done <<< "${output}"

    for key in "${keys[@]}"; do
        [[ -n "${actual[${key}]+x}" ]] || die "${dataset} did not report property ${key}"
        property_value_is_expected "${expected[${key}]}" "${actual[${key}]}" ||
            die "${dataset} has ${key}=${actual[${key}]}, expected ${expected[${key}]} (properties are never changed in place)"
    done
}

bounded_sample() {
    local path="$1"
    local sample

    if ! sample="$(find "${path}" -xdev -mindepth 1 -maxdepth 1 ! -path "${path}/.zfs" -print -quit)"; then
        die "unable to select a bounded sample under ${path}"
    fi
    printf '%s' "${sample}"
}

stat_value() {
    local format="$1"
    local path="$2"
    local value

    if ! value="$(stat -c "${format}" -- "${path}")"; then
        die "unable to inspect ${path}"
    fi
    printf '%s' "${value}"
}

labels_are_ready() {
    local path="$1"
    local sample

    [[ "$(stat_value '%C' "${path}")" == "${EXPECTED_LABEL}" ]] || return 1
    sample="$(bounded_sample "${path}")"
    [[ -z "${sample}" ]] || label_has_expected_security "$(stat_value '%C' "${sample}")"
}

label_has_expected_security() {
    local label="$1"

    [[ "${label}" == *:"${EXPECTED_LABEL_SUFFIX}" ]] &&
        [[ "${label}" != *:*:*:*:* ]]
}

ownership_uid_is_mapped() {
    local value="$1"
    local range_end=$((10#${SERVICE_SUBID_START} + 10#${SERVICE_SUBID_COUNT}))

    [[ "${value}" =~ ^[0-9]+$ ]] || return 1
    ((10#${value} == 10#${SERVICE_UID})) && return 0
    ((10#${value} >= 10#${SERVICE_SUBID_START} && 10#${value} < range_end))
}

ownership_gid_is_mapped() {
    local value="$1"
    local supplemental_gid
    local range_end=$((10#${SERVICE_SUBID_START} + 10#${SERVICE_SUBID_COUNT}))

    [[ "${value}" =~ ^[0-9]+$ ]] || return 1
    ((10#${value} == 10#${SERVICE_GID})) && return 0
    for supplemental_gid in "${SERVICE_SUPPLEMENTAL_GIDS[@]}"; do
        ((10#${value} == 10#${supplemental_gid})) && return 0
    done
    ((10#${value} >= 10#${SERVICE_SUBID_START} && 10#${value} < range_end))
}

ownership_pair_is_mapped() {
    local pair="$1"
    local uid="${pair%%:*}"
    local gid="${pair#*:}"

    [[ "${pair}" == *:* && "${gid}" != *:* ]] || return 1
    ownership_uid_is_mapped "${uid}" && ownership_gid_is_mapped "${gid}"
}

report_label_mismatch() {
    local path="$1"
    local sample
    local value

    value="$(stat_value '%C' "${path}")"
    if [[ "${value}" != "${EXPECTED_LABEL}" ]]; then
        log "drift: ${path} label is ${value}; expected ${EXPECTED_LABEL}"
    fi
    sample="$(bounded_sample "${path}")"
    if [[ -n "${sample}" ]]; then
        value="$(stat_value '%C' "${sample}")"
        if ! label_has_expected_security "${value}"; then
            log "drift: ${sample} label is ${value}; expected any SELinux user with ${EXPECTED_LABEL_SUFFIX} and no MCS categories"
        fi
    fi
}

report_owned_state_mismatch() {
    local path="$1"
    local mode="$2"
    local sample
    local value

    value="$(stat_value '%u:%g' "${path}")"
    if [[ "${value}" != "${SERVICE_UID}:${SERVICE_GID}" ]]; then
        log "drift: ${path} owner is ${value}; expected ${SERVICE_UID}:${SERVICE_GID}"
    fi
    value="$(stat_value '%a' "${path}")"
    if [[ "${value}" != "${mode#0}" ]]; then
        log "drift: ${path} mode is ${value}; expected ${mode#0}"
    fi
    report_label_mismatch "${path}"
    sample="$(bounded_sample "${path}")"
    if [[ -n "${sample}" ]]; then
        value="$(stat_value '%u:%g' "${sample}")"
        if ! ownership_pair_is_mapped "${value}"; then
            log "drift: ${sample} owner is ${value}; expected service IDs, declared supplemental GIDs (${SERVICE_SUPPLEMENTAL_GIDS[*]:--}), or IDs in ${SERVICE_SUBID_START}..$((10#${SERVICE_SUBID_START} + 10#${SERVICE_SUBID_COUNT} - 1))"
        fi
    fi
}

owned_state_is_ready() {
    local path="$1"
    local mode="$2"
    local sample

    [[ "$(stat_value '%u:%g' "${path}")" == "${SERVICE_UID}:${SERVICE_GID}" ]] || return 1
    [[ "$(stat_value '%a' "${path}")" == "${mode#0}" ]] || return 1
    labels_are_ready "${path}" || return 1
    sample="$(bounded_sample "${path}")"
    [[ -z "${sample}" ]] || ownership_pair_is_mapped "$(stat_value '%u:%g' "${sample}")"
}

fcontext_is_ready() {
    local path="$1"
    local expected

    if ! expected="$(matchpathcon -n "${path}")"; then
        return 1
    fi
    [[ "${expected}" == "${EXPECTED_LABEL}" ]] || return 1
    if ! expected="$(matchpathcon -n "${path}/.nas-storage-probe")"; then
        return 1
    fi
    [[ "${expected}" == "${EXPECTED_LABEL}" ]]
}

rootless_podman() {
    runuser -u "${SERVICE_USER}" -- env \
        HOME="/var/home/${SERVICE_USER}" \
        XDG_RUNTIME_DIR="/run/user/${SERVICE_UID}" \
        podman "$@"
}

ensure_mutation_is_safe() {
    local exists_status
    local listeners=""
    local line
    local port
    local running

    if rootless_podman container exists "${SERVICE_CONTAINER}" >/dev/null 2>&1; then
        if ! running="$(rootless_podman inspect "${SERVICE_CONTAINER}" --format '{{.State.Running}}')"; then
            die "unable to inspect ${SERVICE_CONTAINER} in ${SERVICE_USER}'s Podman store"
        fi
        [[ "${running}" != "true" ]] || die "refusing storage mutation while ${SERVICE_CONTAINER} is running"
    else
        exists_status=$?
        [[ "${exists_status}" -eq 1 ]] || die "unable to query ${SERVICE_USER}'s Podman store"
    fi

    if ((${#SERVICE_PORTS[@]} > 0)); then
        if ! listeners="$(ss -H -ltn)"; then
            die "unable to inspect listening TCP ports"
        fi
        while IFS= read -r line; do
            for port in "${SERVICE_PORTS[@]}"; do
                [[ ! "${line}" =~ :${port}([[:space:]]|$) ]] ||
                    die "refusing storage mutation while a process listens on TCP port ${port}"
            done
        done <<< "${listeners}"
    fi
}

ensure_fcontext_rule() {
    local path="$1"
    local target="${path//./\\.}(/.*)?"

    if semanage fcontext -a -t container_file_t -r s0 "${target}" 2>/dev/null; then
        log "added SELinux fcontext for ${path}"
        return
    fi
    semanage fcontext -m -t container_file_t -r s0 "${target}" ||
        die "unable to install SELinux fcontext for ${path}"
}

restorecon_recursive() {
    local path="$1"
    local -a arguments=(-F -R -x)

    if [[ -d "${path}/.zfs" ]]; then
        arguments+=(-e "${path}/.zfs")
    fi
    restorecon "${arguments[@]}" "${path}" || die "unable to restore SELinux labels under ${path}"
}

create_managed_dataset() {
    local index="$1"
    local dataset="${ENTRY_DATASET[index]}"
    local path="${ENTRY_PATH[index]}"
    local properties="${ENTRY_PROPERTIES[index]}"
    local property
    local -a arguments=(create -o "mountpoint=${path}")
    local -a requested=()

    if [[ "${properties}" != "-" ]]; then
        IFS=',' read -r -a requested <<< "${properties}"
        for property in "${requested[@]}"; do
            arguments+=(-o "${property}")
        done
    fi
    zfs "${arguments[@]}" "${dataset}" || die "unable to create managed dataset ${dataset}"
    AVAILABLE_DATASETS["${dataset}"]=1
}

repair_descendant_ownership() {
    local path="$1"
    local supplemental_gid
    local range_end=$((10#${SERVICE_SUBID_START} + 10#${SERVICE_SUBID_COUNT}))
    local range_lower=$((10#${SERVICE_SUBID_START} - 1))
    local -a allowed_gids=(-gid "${SERVICE_GID}")

    for supplemental_gid in "${SERVICE_SUPPLEMENTAL_GIDS[@]}"; do
        allowed_gids+=(-o -gid "${supplemental_gid}")
    done

    find "${path}" -xdev -path "${path}/.zfs" -prune -o -mindepth 1 \
        \( ! \( -uid "${SERVICE_UID}" -o \
                 \( -uid "+${range_lower}" -a -uid "-${range_end}" \) \) \
           -o ! \( "${allowed_gids[@]}" -o \
                    \( -gid "+${range_lower}" -a -gid "-${range_end}" \) \) \) \
        -exec chown -h "${SERVICE_UID}:${SERVICE_GID}" {} + ||
        die "unable to repair ownership under ${path}"
}

verify_descendant_ownership() {
    local path="$1"
    local supplemental_gid
    local range_end=$((10#${SERVICE_SUBID_START} + 10#${SERVICE_SUBID_COUNT}))
    local range_lower=$((10#${SERVICE_SUBID_START} - 1))
    local unexpected
    local -a allowed_gids=(-gid "${SERVICE_GID}")

    for supplemental_gid in "${SERVICE_SUPPLEMENTAL_GIDS[@]}"; do
        allowed_gids+=(-o -gid "${supplemental_gid}")
    done

    if ! unexpected="$(find "${path}" -xdev -path "${path}/.zfs" -prune -o \
        -mindepth 1 \
        \( ! \( -uid "${SERVICE_UID}" -o \
                 \( -uid "+${range_lower}" -a -uid "-${range_end}" \) \) \
           -o ! \( "${allowed_gids[@]}" -o \
                    \( -gid "+${range_lower}" -a -gid "-${range_end}" \) \) \) \
        -print -quit)"; then
        die "unable to verify ownership under ${path}"
    fi
    [[ -z "${unexpected}" ]] || die "${unexpected} has unexpected ownership"
}

declare -A AVAILABLE_POOLS=()
declare -A AVAILABLE_DATASETS=()

(($# == 1)) || usage
MANIFEST="$1"
is_absolute_path "${MANIFEST}" || die "manifest path must be an absolute normalized path"
[[ "${MANIFEST}" == *.storage-manifest ]] || die "manifest path must end in .storage-manifest"
[[ -f "${MANIFEST}" && ! -L "${MANIFEST}" ]] || die "manifest must be a regular, non-symlink file"

# Parsing is deliberately completed before command discovery or any host-state
# inspection. The manifest is data, never shell input, and is never sourced.
parse_manifest "${MANIFEST}"

RUNTIME_ROOT="/run/nas-storage"
REPAIR_ROOT="/var/lib/nas-repairs"
if [[ -n "${NAS_STORAGE_RUNTIME_ROOT:-}" ]]; then
    ((EUID != 0)) || die "NAS_STORAGE_RUNTIME_ROOT is only available to non-root test runs"
    is_absolute_path "${NAS_STORAGE_RUNTIME_ROOT}" || die "invalid test runtime root"
    RUNTIME_ROOT="${NAS_STORAGE_RUNTIME_ROOT}"
fi
if [[ -n "${NAS_STORAGE_REPAIR_ROOT:-}" ]]; then
    ((EUID != 0)) || die "NAS_STORAGE_REPAIR_ROOT is only available to non-root test runs"
    is_absolute_path "${NAS_STORAGE_REPAIR_ROOT}" || die "invalid test repair root"
    REPAIR_ROOT="${NAS_STORAGE_REPAIR_ROOT}"
fi
READY_DIR="${RUNTIME_ROOT}/${SERVICE_NAME}"
READY_FILE="${READY_DIR}/ready"
REPAIR_REQUEST="${REPAIR_ROOT}/${SERVICE_NAME}/repair-required"

# Readiness is owned by this process, not by a systemd ExecStartPost. Clearing
# it here prevents a failed same-boot rerun from leaving stale success behind.
rm -f -- "${READY_FILE}"

require_commands

if ! getent passwd "${SERVICE_USER}" >/dev/null; then
    die "service user '${SERVICE_USER}' does not exist"
fi
[[ "$(id -u "${SERVICE_USER}")" == "${SERVICE_UID}" ]] || die "${SERVICE_USER} has an unexpected UID"
[[ "$(id -g "${SERVICE_USER}")" == "${SERVICE_GID}" ]] || die "${SERVICE_USER} has an unexpected GID"

if [[ "${HAS_ZFS}" -eq 1 ]]; then
    load_zfs_inventory
fi

for dataset in "${ENTRY_DATASET[@]}"; do
    [[ "${dataset}" == "-" ]] && continue
    pool="$(dataset_pool "${dataset}")"
    [[ -n "${AVAILABLE_POOLS[${pool}]+x}" ]] || die "required pool '${pool}' does not exist"
done

# Complete all existence, mount, and immutable-property checks before planning
# or performing a mutation.
for index in "${!ENTRY_KIND[@]}"; do
    kind="${ENTRY_KIND[index]}"
    dataset="${ENTRY_DATASET[index]}"
    path="${ENTRY_PATH[index]}"
    case "${kind}" in
        directory)
            if [[ -e "${path}" ]]; then
                [[ -d "${path}" && ! -L "${path}" ]] || die "${path} is not a real directory"
            else
                ENTRY_WAS_MISSING[index]=1
            fi
            ;;
        managed-zfs)
            if dataset_exists "${dataset}"; then
                verify_dataset_properties "${index}"
                if [[ "${path}" != "none" ]]; then
                    [[ -d "${path}" && ! -L "${path}" ]] || die "managed mount path ${path} is not a real directory"
                    validate_mount "${dataset}" "${path}"
                fi
            else
                ENTRY_WAS_MISSING[index]=1
            fi
            ;;
    esac
done

requested_repair=0
if [[ -e "${REPAIR_REQUEST}" ]]; then
    [[ -f "${REPAIR_REQUEST}" && ! -L "${REPAIR_REQUEST}" ]] || die "repair marker must be a regular, non-symlink file"
    requested_repair=1
fi

bootstrap_needed=0
repair_needed="${requested_repair}"
policy_needed=0
for index in "${!ENTRY_KIND[@]}"; do
    path="${ENTRY_PATH[index]}"
    [[ "${ENTRY_WAS_MISSING[index]}" -eq 0 ]] || bootstrap_needed=1
    [[ "${path}" != "none" ]] || continue

    if ! fcontext_is_ready "${path}"; then
        policy_needed=1
    fi
    [[ "${ENTRY_WAS_MISSING[index]}" -eq 0 ]] || continue

    if [[ "$(stat_value '%u:%g' "${path}")" == "0:0" ]]; then
        repair_needed=1
        log "detected interrupted repair marker on ${path}"
    elif ! owned_state_is_ready "${path}" "${ENTRY_MODE[index]}"; then
        repair_needed=1
        report_owned_state_mismatch "${path}" "${ENTRY_MODE[index]}"
        if [[ "${HAS_ZFS}" -eq 1 && "${requested_repair}" -ne 1 ]]; then
            repair_required "${path} needs an explicit repair request at ${REPAIR_REQUEST}"
        fi
    fi
done

if [[ "${bootstrap_needed}" -eq 1 || "${repair_needed}" -eq 1 || "${policy_needed}" -eq 1 ]]; then
    ensure_mutation_is_safe
fi

# Materialize only resources explicitly declared as managed. Creation order is
# manifest order, so the generator owns parent-before-child ordering.
for index in "${!ENTRY_KIND[@]}"; do
    [[ "${ENTRY_WAS_MISSING[index]}" -eq 1 ]] || continue
    kind="${ENTRY_KIND[index]}"
    case "${kind}" in
        directory)
            install -d -m "${ENTRY_MODE[index]}" -o "${SERVICE_UID}" -g "${SERVICE_GID}" "${ENTRY_PATH[index]}"
            ;;
        managed-zfs)
            create_managed_dataset "${index}"
            ;;
    esac
done

# A created ZFS dataset must satisfy the same immutable contract immediately.
for index in "${!ENTRY_KIND[@]}"; do
    [[ "${ENTRY_KIND[index]}" == "managed-zfs" ]] || continue
    verify_dataset_properties "${index}"
    if [[ "${ENTRY_PATH[index]}" != "none" ]]; then
        [[ -d "${ENTRY_PATH[index]}" && ! -L "${ENTRY_PATH[index]}" ]] || die "created mount path is not a real directory"
        validate_mount "${ENTRY_DATASET[index]}" "${ENTRY_PATH[index]}"
    fi
done

for index in "${!ENTRY_KIND[@]}"; do
    path="${ENTRY_PATH[index]}"
    [[ "${path}" != "none" ]] || continue
    if ! fcontext_is_ready "${path}"; then
        ensure_fcontext_rule "${path}"
    fi
done

if [[ "${repair_needed}" -eq 1 ]]; then
    log "running guarded full repair for ${SERVICE_NAME}"

    # Arm every owned root before recursive work. A root left as 0:0 is the
    # durable signal that a later invocation must resume the full repair.
    for index in "${!ENTRY_KIND[@]}"; do
        [[ "${ENTRY_PATH[index]}" != "none" ]] || continue
        chown root:root "${ENTRY_PATH[index]}"
    done

    for index in "${!ENTRY_KIND[@]}"; do
        path="${ENTRY_PATH[index]}"
        [[ "${path}" != "none" ]] || continue
        repair_descendant_ownership "${path}"
        verify_descendant_ownership "${path}"
        restorecon_recursive "${path}"
    done

    for index in "${!ENTRY_KIND[@]}"; do
        [[ "${ENTRY_PATH[index]}" != "none" ]] || continue
        chown "${SERVICE_UID}:${SERVICE_GID}" "${ENTRY_PATH[index]}"
        chmod "${ENTRY_MODE[index]}" "${ENTRY_PATH[index]}"
    done
else
    # Newly created paths are empty. Preparing just those roots does not turn
    # an ordinary boot into an unbounded scan over existing application data.
    for index in "${!ENTRY_KIND[@]}"; do
        [[ "${ENTRY_WAS_MISSING[index]}" -eq 1 ]] || continue
        path="${ENTRY_PATH[index]}"
        [[ "${path}" != "none" ]] || continue
        restorecon_recursive "${path}"
        chown "${SERVICE_UID}:${SERVICE_GID}" "${path}"
        chmod "${ENTRY_MODE[index]}" "${path}"
    done
fi

for index in "${!ENTRY_KIND[@]}"; do
    path="${ENTRY_PATH[index]}"
    [[ "${path}" != "none" ]] || continue
    fcontext_is_ready "${path}" || die "SELinux policy does not cover ${path}"
    owned_state_is_ready "${path}" "${ENTRY_MODE[index]}" || die "${path} has unexpected bounded ownership, mode, or SELinux state"
done

if [[ "${requested_repair}" -eq 1 ]]; then
    rm -f -- "${REPAIR_REQUEST}"
    rmdir -- "${REPAIR_REQUEST%/*}" 2>/dev/null || true
fi

# Publish the boot ID atomically only after every postcondition succeeds.
install -d -m 0755 -o root -g root "${READY_DIR}"
ready_temporary="${READY_FILE}.$$"
umask 0022
if ! read -r boot_id < /proc/sys/kernel/random/boot_id; then
    die "unable to read the current boot ID"
fi
printf '%s\n' "${boot_id}" > "${ready_temporary}"
mv -f -- "${ready_temporary}" "${READY_FILE}"

log "${SERVICE_NAME} storage is ready"
