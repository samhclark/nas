#!/usr/bin/env bash

# Collect read-only diagnostics for boot failures involving the generated
# libkrun TAP network policy and systemd-networkd-wait-online.

set -uo pipefail

export LC_ALL=C
export SYSTEMD_COLORS=0
export SYSTEMD_PAGER=cat

TAP_MANIFEST=/usr/share/nas/fleet/active-taps.tsv
HOST_TAP_MANIFEST=/usr/share/nas/fleet/host-vm-taps.tsv

if (( EUID != 0 )); then
    echo "Run this script as root (for example: sudo bash $0)." >&2
    exit 1
fi

for manifest in "${TAP_MANIFEST}" "${HOST_TAP_MANIFEST}"; do
    if [[ ! -r "${manifest}" ]]; then
        echo "Fleet manifest is missing: ${manifest}" >&2
        exit 1
    fi
done

TAPS=()
USER_UNITS=()
ACCOUNT_UNITS=()
while IFS=$'\t' read -r tap user_unit account_unit; do
    [[ "${tap}" == \#* ]] && continue
    TAPS+=("${tap}")
    USER_UNITS+=("${user_unit}")
    ACCOUNT_UNITS+=("${account_unit}")
done < "${TAP_MANIFEST}"
while IFS=$'\t' read -r name tap _; do
    [[ "${name}" == \#* ]] && continue
    TAPS+=("${tap}")
done < "${HOST_TAP_MANIFEST}"

ACCOUNT_JOURNAL_ARGS=()
USER_JOURNAL_ARGS=()
for unit in "${ACCOUNT_UNITS[@]}"; do
    ACCOUNT_JOURNAL_ARGS+=(-u "$unit")
done
for unit in "${USER_UNITS[@]}"; do
    USER_JOURNAL_ARGS+=(-u "$unit")
done

section() {
    printf '\n===== %s =====\n' "$1"
}

run() {
    local description=$1
    shift
    section "$description"
    "$@" 2>&1
    local status=$?
    if (( status != 0 )); then
        printf '[command exited with status %d]\n' "$status"
    fi
    return 0
}

section "collection metadata"
printf 'collected_at=%s\n' "$(date --iso-8601=seconds)"
printf 'hostname=%s\n' "$(hostname)"
printf 'boot_id=%s\n' "$(cat /proc/sys/kernel/random/boot_id)"
printf 'kernel=%s\n' "$(uname -r)"
printf 'os_release='
sed -n 's/^PRETTY_NAME=//p' /etc/os-release
printf 'systemd=%s\n' "$(systemctl --version | head -n 1)"

run "failed units" systemctl --failed --no-pager --full
run "outstanding systemd jobs" systemctl list-jobs --no-pager --full
run "network targets and services" systemctl status --no-pager --full \
    network-pre.target network.target network-online.target \
    NetworkManager.service systemd-networkd.service \
    systemd-networkd-wait-online.service nftables.service \
    nas-krun-network-policy.service

run "policy unit definition" systemctl cat nas-krun-network-policy.service
run "wait-online unit definition" systemctl cat systemd-networkd-wait-online.service
run "networkd unit definition and drop-ins" systemctl cat systemd-networkd.service
run "policy unit properties" systemctl show nas-krun-network-policy.service \
    -p ActiveState -p SubState -p Result -p ExecMainCode -p ExecMainStatus \
    -p ExecMainStartTimestamp -p ExecMainExitTimestamp -p TimeoutStartUSec \
    -p InvocationID -p Requires -p Wants -p After
run "wait-online unit properties" systemctl show systemd-networkd-wait-online.service \
    -p ActiveState -p SubState -p Result -p ExecMainCode -p ExecMainStatus \
    -p ExecStart -p InvocationID -p Requires -p Wants -p After
run "boot critical chain for policy" systemd-analyze critical-chain \
    nas-krun-network-policy.service
run "boot critical chain for network-online" systemd-analyze critical-chain \
    network-online.target

run "policy and network boot journal" journalctl -b --no-pager \
    -o short-monotonic \
    -u nas-krun-network-policy.service \
    -u systemd-networkd.service \
    -u systemd-networkd-wait-online.service \
    -u nftables.service
if (( ${#ACCOUNT_JOURNAL_ARGS[@]} > 0 )); then
    run "account preparation boot journal" journalctl -b --no-pager \
        -o short-monotonic "${ACCOUNT_JOURNAL_ARGS[@]}"
else
    section "account preparation boot journal"
    echo "No active TAP service accounts are declared."
fi

run "networkd link summary" networkctl list --no-pager --all
run "networkd overall status" networkctl status --no-pager --all
run "IPv4 addresses" ip -4 -details address show
run "IPv4 routes in all tables" ip -4 route show table all
run "IPv4 policy rules" ip -4 rule show

for tap in "${TAPS[@]}"; do
    run "networkd status: ${tap}" networkctl status --no-pager "$tap"
    run "link details: ${tap}" ip -details link show dev "$tap"
    run "IPv4 address: ${tap}" ip -4 -o address show dev "$tap"

    section "network configuration files: ${tap}"
    found=0
    for directory in /run/systemd/network /etc/systemd/network /usr/lib/systemd/network; do
        for file in "${directory}"/*"${tap}"*; do
            [[ -f "$file" ]] || continue
            found=1
            printf '%s\n' "--- ${file}"
            sed -n '1,240p' "$file"
        done
    done
    if (( found == 0 )); then
        echo "No matching network configuration file found."
    fi

    section "sysfs state: ${tap}"
    if [[ -d "/sys/class/net/${tap}" ]]; then
        for property in ifindex operstate carrier; do
            printf '%s=' "$property"
            cat "/sys/class/net/${tap}/${property}" 2>&1 || true
        done
    else
        echo "Interface is absent."
    fi
done

section "policy readiness marker"
if [[ -e /run/nas-krun-network/policy-ready ]]; then
    stat /run/nas-krun-network/policy-ready
    printf 'contents='
    cat /run/nas-krun-network/policy-ready
    printf 'current_boot_id='
    cat /proc/sys/kernel/random/boot_id
else
    echo "/run/nas-krun-network/policy-ready is absent."
fi

run "required nftables objects" bash -c '
    nft list chain inet filter nas_krun_input
    nft list chain inet filter nas_krun_forward
    nft list table ip nas_krun_nat
'
run "complete nftables ruleset" nft list ruleset

if (( ${#USER_UNITS[@]} > 0 )); then
    run "service user manager states" systemctl show "${USER_UNITS[@]}" \
        -p Id -p LoadState -p ActiveState -p SubState -p Result -p Job -p InvocationID
    run "service user manager status" systemctl status --no-pager --full \
        "${USER_UNITS[@]}"
    run "service user manager boot journal" journalctl -b --no-pager \
        -o short-monotonic "${USER_JOURNAL_ARGS[@]}"
else
    section "service user managers"
    echo "No active TAP services are declared."
fi
run "logged-in and lingering users" loginctl list-users --no-pager

WAIT_ONLINE=/usr/lib/systemd/systemd-networkd-wait-online
if [[ ! -x "$WAIT_ONLINE" ]]; then
    WAIT_ONLINE=$(command -v systemd-networkd-wait-online || true)
fi

section "wait-online implementation"
if [[ -n "$WAIT_ONLINE" && -x "$WAIT_ONLINE" ]]; then
    printf 'path=%s\n' "$WAIT_ONLINE"
    "$WAIT_ONLINE" --version 2>&1 || true
    "$WAIT_ONLINE" --help 2>&1 || true

    policy_interfaces=()
    for tap in "${TAPS[@]}"; do
        policy_interfaces+=("--interface=${tap}:off")
    done

    if (( ${#policy_interfaces[@]} > 0 )); then
        run "10-second reproduction of policy wait (debug logging)" \
            timeout --signal=TERM 15s env SYSTEMD_LOG_LEVEL=debug \
            "$WAIT_ONLINE" --timeout=10 --ipv4 "${policy_interfaces[@]}"
    else
        echo "No active TAP interfaces are declared; skipping policy wait reproduction."
    fi
    run "10-second reproduction of global wait (debug logging)" \
        timeout --signal=TERM 15s env SYSTEMD_LOG_LEVEL=debug \
        "$WAIT_ONLINE" --timeout=10
else
    echo "systemd-networkd-wait-online executable not found."
fi

section "end of diagnostics"
echo "No services, interfaces, routes, rules, or files were changed."
