#!/usr/bin/env bash
# ABOUTME: Runs read-only assertions inside the exact bootc image produced by a build.
set -euo pipefail

readonly expected_kernel="${1:?expected kernel version is required}"
readonly expected_zfs="${2:?expected ZFS version is required}"

actual_kernel="$(rpm -qa kernel --queryformat '%{VERSION}-%{RELEASE}.%{ARCH}')"
actual_zfs="$(rpm -q zfs --queryformat '%{VERSION}')"
[[ "${actual_kernel}" == "${expected_kernel}" ]]
[[ "${actual_zfs}" == "${expected_zfs}" ]]
modinfo -k "${expected_kernel}" zfs >/dev/null

bootc container lint
[[ -d /usr/local && ! -L /usr/local ]]
[[ "$(readlink /usr/bin/krun)" == "crun" ]]
/usr/bin/crun --version | grep -Fq 'crun version 1.29.1'
grep -aFq 'krun.tap_name' /usr/bin/crun
/usr/local/bin/sops --version | grep -Fq 'sops 3.13.3'
/usr/local/bin/vector --version | grep -Fq 'vector 0.57.0'
/usr/local/bin/vector validate \
    --config-yaml /etc/vector/vector.yaml \
    --no-environment --skip-healthchecks
test -x /usr/local/bin/nas-diagnose-immich
test -x \
    /usr/share/nas/immich-database/immich-database-entrypoint.sh
for adapter in \
    /usr/share/nas/sonarr/sonarr-entrypoint.sh \
    /usr/share/nas/radarr/radarr-entrypoint.sh \
    /usr/share/nas/prowlarr/prowlarr-entrypoint.sh \
    /usr/share/nas/sabnzbd/sabnzbd-entrypoint.sh; do
    test -x "${adapter}"
done

semodule -l | grep -Eq '^nas-krun-tun[[:space:]]'

systemd-analyze verify \
    /etc/systemd/system/*.service \
    /etc/systemd/system/*.timer
systemd-sysusers --dry-run --root=/
systemd-tmpfiles --create --dry-run --root=/
/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun \
    >/dev/null

while IFS= read -r unit; do
    [[ -z "${unit}" || "${unit}" == \#* ]] && continue
    [[ "$(systemctl is-enabled "${unit}")" == "enabled" ]]
done < /usr/share/nas/fleet/account-units.list

while IFS= read -r unit; do
    [[ -z "${unit}" || "${unit}" == \#* ]] && continue
    [[ "$(systemctl is-enabled "${unit}")" == "enabled" ]]
done < /usr/share/nas/fleet/storage-units.list

while IFS= read -r unit; do
    [[ -z "${unit}" || "${unit}" == \#* ]] && continue
    [[ "$(systemctl is-enabled "${unit}")" == "enabled" ]]
done < /usr/share/nas/fleet/egress-units.list

for unit in \
    bootc-fetch-apply-updates.timer \
    disk-health-metrics.timer \
    nas-krun-network-policy.service \
    nftables.service \
    node_exporter.service \
    nas-vector.service \
    sops-distribute-secrets.service \
    systemd-networkd.service \
    tailscaled.service; do
    [[ "$(systemctl is-enabled "${unit}")" == "enabled" ]]
done

for unit in fwupd-refresh.timer systemd-networkd-wait-online.service zincati.service; do
    [[ "$(systemctl is-enabled "${unit}")" == "disabled" ]]
done

[[ ! -e /tests ]]
[[ ! -e /docs ]]
[[ ! -e /quadlets ]]
[[ ! -e /usr/share/nas/tests ]]
[[ ! -e /usr/share/nas/history ]]
