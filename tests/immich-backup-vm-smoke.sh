#!/bin/bash
# ABOUTME: Disposable guest fixture for the local Immich backup VM smoke.
set -Eeuo pipefail
readonly WORK=/run/nas-immich-backup-vm-smoke
readonly SECRETS=${WORK}/secrets
readonly RESTIC_IMAGE='docker.io/restic/restic:0.19.1@sha256:08916bcda4a4435f9d9828ebb4e91bb7ada3d2c8a53699788930e0ae1bd4fa67'
readonly RESTIC_CONFIG='sha256:a626e3712f1361f9eeac31b2f62c887f9ff8c38d293611ad7286edca1d99e082'
readonly SOURCE=/var/lib/immich/library
readonly REPOSITORY=/var/lib/nas-backups/immich/restic
readonly PASSWORD=${SECRETS}/immich-backup-restic-password
readonly RESTORE=${WORK}/restore
readonly FIXTURE_DF=${WORK}/df
readonly SCRATCH=/dev/disk/by-id/virtio-scratch
readonly SEED=/run/virtiofs-mnt-seed
readonly DUMP=${SOURCE}/backups/immich-db-smoke.sql.gz
readonly ORIGINAL=${SOURCE}/representative-original.jpg
readonly NESTED=${SOURCE}/upload/library/nested/nested-original.jpg

fail() {
    local status=$?
    trap - ERR TERM
    zpool export -f tank >/dev/null 2>&1 || true
    printf 'NAS_IMMICH_BACKUP_VM_SMOKE_FAIL status=%s\n' "${status}"
    exit "${status}"
}
terminate() {
    trap - ERR TERM
    printf 'NAS_IMMICH_BACKUP_VM_SMOKE_FAIL signal=TERM\n'
    exit 124
}
trap fail ERR
trap terminate TERM

printf 'NAS_IMMICH_BACKUP_VM_SMOKE_BEGIN\n'
[[ "$(getenforce)" == Permissive ]]
install -d -m 0700 "${WORK}"
export CONTAINERS_STORAGE_CONF="${SEED}/storage.conf"
[[ -c /dev/kvm ]]
[[ -c /dev/net/tun ]]
command -v ip >/dev/null
# bcvk gives QEMU a slirp interface, but its enclosing Podman network is
# `none`. Prove that the guest cannot establish an outbound TCP connection.
if timeout 3 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>/dev/null; then
    printf 'Disposable backup VM unexpectedly has outbound connectivity.\n' >&2
    false
fi
[[ "$(/usr/bin/crun --version | head -1)" == *'crun version 1.29.1'* ]]
modprobe zfs
[[ -d /sys/module/zfs ]]
modinfo -F vermagic zfs | grep -Fq "$(uname -r)"
[[ -b "${SCRATCH}" ]]

# The root image and this disk are both disposable. The production
# pool is never visible inside this VM and the scratch disk is always
# attached as a separate QEMU virtio disk.
zpool create -f -m none -o cachefile=none tank "${SCRATCH}"
zfs create -o mountpoint=none tank/immich-server
zfs create -o mountpoint=/var/lib/immich/library \
    -o snapdir=visible -o recordsize=128K -o compression=lz4 \
    -o atime=off tank/immich-server/library
install -d -m 0750 -o 51130 -g 51130 /var/lib/immich
chown 51130:51130 "${SOURCE}"
chmod 0750 "${SOURCE}"
source_fcontext="${SOURCE//./\\.}(/.*)?"
semanage fcontext -a -t container_file_t -r s0 "${source_fcontext}" 2>/dev/null || \
    semanage fcontext -m -t container_file_t -r s0 "${source_fcontext}"
restorecon -F -R -x "${SOURCE}"
matchpathcon -V "${SOURCE}"
[[ "$(stat -c %C "${SOURCE}")" == system_u:object_r:container_file_t:s0 ]]
ls -Zd -- "${SOURCE}"
install -d -m 0750 -o 51130 -g 51130 "${SOURCE}/backups"
install -d -m 0750 -o 51130 -g 51130 "${SOURCE}/upload/library/nested"
printf '%s\n' 'nas-backup-vm-smoke database fixture' \
    | gzip -c > "${DUMP}"
chown 51130:51130 "${DUMP}"
printf '%s\n' 'nas-backup-vm-smoke-original-content' \
    > "${ORIGINAL}"
printf '%s\n' 'nas-backup-vm-smoke-nested-content' \
    > "${NESTED}"
setfattr -n user.nas_backup_vm_smoke -v preserved "${NESTED}"
chown 51130:51130 "${ORIGINAL}" "${NESTED}"
expected_files=3
expected_bytes=$(stat -c %s "${DUMP}" "${ORIGINAL}" "${NESTED}" \
    | awk '{total += $1} END {print total}')

install -d -m 0700 -o root -g root "${SECRETS}"
printf '%s\n' fixture-only-restic-password > "${PASSWORD}"
printf '%s\n' fixture-only-b2-key-id > "${SECRETS}/immich-backup-b2-key-id"
printf '%s\n' fixture-only-b2-application-key > \
    "${SECRETS}/immich-backup-b2-application-key"
printf '%s\n' fixture-only-b2-bucket > "${SECRETS}/immich-backup-b2-bucket"
printf '%s\n' https://s3.us-west-004.backblazeb2.com > \
    "${SECRETS}/immich-backup-b2-s3-endpoint"
chmod 0600 "${SECRETS}"/*

# The ephemeral bootc root is intentionally small. Capacity refusal has
# dedicated unit coverage; this fixture supplies a bounded df result so it can
# exercise the backup pipeline against the disposable ZFS source and disk.
printf '%s\n' \
    '#!/bin/bash' \
    "printf 'Avail Size\\n214748364800 268435456000\\n'" > "${FIXTURE_DF}"
chmod 0555 "${FIXTURE_DF}"

# Load only the pinned local-restic fixture image from the read-only bcvk bind.
# No guest network is configured, so --pull=missing cannot
# reach a registry or B2.
[[ -d "${SEED}" ]]
set +e
skopeo copy --preserve-digests \
    "oci-archive:$(find "${SEED}" -name restic.oci.tar -print -quit)" \
    containers-storage:docker.io/restic/restic:0.19.1
image_import_status=$?
image_inventory="$(podman images --digests --no-trunc \
    --format '{{.Repository}}|{{.Tag}}|{{.Digest}}|{{.ID}}')"
image_inventory_status=$?
set -e
printf 'Pinned image inventory (status=%s):\n%s\n' \
    "${image_inventory_status}" "${image_inventory}"
printf '%s\n' "${image_inventory}" > "${WORK}/image-inventory.txt"
expected_inventory="docker.io/restic/restic|0.19.1|${RESTIC_IMAGE##*@}|${RESTIC_CONFIG}"
grep -Fxq -- "${expected_inventory}" "${WORK}/image-inventory.txt"
if [[ "${image_import_status}" -ne 0 ]]; then
    printf 'Image importer returned %s after installing the verified pinned image.\n' \
        "${image_import_status}"
fi

# The normal storage preparation is part of the exact production
# runner. It labels only the disposable repository below /var.
backup_env=(env
    DF_BIN="${FIXTURE_DF}"
    NAS_BACKUP_SECRET_DIR="${SECRETS}"
    NAS_BACKUP_HOST_TAP_MANIFEST="${WORK}/intentionally-missing-host-vm-taps.tsv"
    NAS_BACKUP_METRICS_FILE="${WORK}/backup.prom")
trap - ERR
set +e
"${backup_env[@]}" \
    /usr/local/bin/nas-backup-immich init > "${WORK}/init.log" 2>&1
init_status=$?
"${backup_env[@]}" \
    /usr/local/bin/nas-backup-immich run > "${WORK}/backup.log" 2>&1
backup_status=$?
set -e
trap fail ERR
cat "${WORK}/init.log"
cat "${WORK}/backup.log"
# The local stage must complete, then the outbound stage must fail
# closed before launching a networked guest because its manifest is
# deliberately absent.
[[ "${init_status}" -ne 0 ]]
[[ "${backup_status}" -ne 0 ]]
grep -Fq 'created restic repository' "${WORK}/init.log"
grep -Fq 'Host-VM TAP manifest is unavailable' "${WORK}/init.log"
grep -Fq 'Host-VM TAP manifest is unavailable' "${WORK}/backup.log"
grep -Fq 'Validated latest restic snapshot content:' "${WORK}/backup.log"
[[ -s /var/lib/nas-backups/immich/state/local-success ]]
[[ ! -e /var/lib/nas-backups/immich/state/remote-success ]]
grep -Fq 'nas_backup_last_run_success{application="immich"} 0' \
    "${WORK}/backup.prom"
grep -Fq 'nas_backup_last_success_timestamp_seconds{application="immich",destination="b2"} 0' \
    "${WORK}/backup.prom"
[[ -z "$(zfs list -H -t snapshot -o name tank/immich-server/library \
    | grep -F 'nas-backup-immich-' || true)" ]]

# Commas belong inside the single --tmpfs argument below.
# shellcheck disable=SC2054
common=(podman run --rm --pull=never --runtime=krun \
    --annotation=krun.cpus=2 --annotation=krun.ram_mib=2048 \
    --network=none --read-only --cap-drop=all \
    --security-opt=no-new-privileges --pids-limit=512 \
    --cpu-shares=128 --blkio-weight=100 \
    --tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m \
    --env=HOME=/tmp --env=XDG_CACHE_HOME=/tmp/cache \
    --env=RESTIC_REPOSITORY=/repository \
    --env=RESTIC_PASSWORD_FILE=/run/secrets/restic-password \
    --volume="${REPOSITORY}:/repository:ro" \
    --volume="${PASSWORD}:/run/secrets/restic-password:ro,Z")
snapshot_json="$("${common[@]}" "${RESTIC_IMAGE}" \
    snapshots --no-lock --json --latest=1)"
files="$(jq -r '.[0].summary.total_files_processed // 0' <<<"${snapshot_json}")"
bytes="$(jq -r '.[0].summary.total_bytes_processed // 0' <<<"${snapshot_json}")"
[[ "${files}" == "${expected_files}" ]]
[[ "${bytes}" == "${expected_bytes}" ]]

install -d -m 0755 "${RESTORE}"
"${common[@]}" \
    --cap-add=CHOWN --cap-add=FOWNER \
    --cap-add=DAC_OVERRIDE --cap-add=DAC_READ_SEARCH \
    --volume="${RESTORE}:/restore:rw,Z" \
    "${RESTIC_IMAGE}" restore --no-lock \
        --exclude-xattr=security.selinux latest --target /restore
restored_original="$(find "${RESTORE}" -name representative-original.jpg -print -quit)"
restored_nested="$(find "${RESTORE}" -name nested-original.jpg -print -quit)"
restored_dump="$(find "${RESTORE}" -name immich-db-smoke.sql.gz -print -quit)"
[[ -n "${restored_dump}" && -n "${restored_original}" && -n "${restored_nested}" ]]
gzip -t "${restored_dump}"
cmp <(gzip -cd "${DUMP}") <(gzip -cd "${restored_dump}")
cmp "${ORIGINAL}" "${restored_original}"
cmp "${NESTED}" "${restored_nested}"
[[ "$(stat -c %u:%g "${restored_nested}")" == 51130:51130 ]]
[[ "$(getfattr --only-values -n user.nas_backup_vm_smoke "${restored_nested}")" == preserved ]]

zpool export tank
printf 'NAS_IMMICH_BACKUP_VM_SMOKE_PASS files=%s bytes=%s\n' "${files}" "${bytes}"
