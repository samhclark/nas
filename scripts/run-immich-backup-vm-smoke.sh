#!/usr/bin/env bash
# ABOUTME: Runs the bounded local Immich backup integration smoke in bcvk.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly repo
readonly local_image='nas/bootc:stable'
readonly imported_image='localhost/nas/bootc:stable'
readonly restic_image='docker.io/restic/restic:0.19.1@sha256:08916bcda4a4435f9d9828ebb4e91bb7ada3d2c8a53699788930e0ae1bd4fa67'
readonly container_cli="${CONTAINER_CLI:-podman}"
readonly docker_cli="${DOCKER_BIN:-docker}"
readonly skopeo_bin="${SKOPEO_BIN:-skopeo}"
readonly bcvk_bin="${BCVK_BIN:-bcvk}"
readonly qemu_img="${QEMU_IMG_BIN:-qemu-img}"
readonly timeout_bin="${TIMEOUT_BIN:-timeout}"
readonly fixture="${NAS_BACKUP_VM_FIXTURE:-${repo}/tests/immich-backup-vm-smoke.sh}"

die() {
    printf 'Immich backup VM smoke: %s\n' "$*" >&2
    exit 2
}

restic_archive_is_exact() {
    local archive=$1
    "${skopeo_bin}" inspect "oci-archive:${archive}" 2>/dev/null \
        | "${JQ_BIN:-jq}" --exit-status --arg digest "${restic_digest}" \
            ".Digest == \$digest and .Architecture == \"amd64\" and .Os == \"linux\"" \
            >/dev/null
}

for command_name in "${container_cli}" "${docker_cli}" "${skopeo_bin}" \
        "${bcvk_bin}" "${qemu_img}" "${timeout_bin}" sha256sum; do
    command -v "${command_name}" >/dev/null || die "${command_name} is required"
done
[[ -r /dev/kvm && -w /dev/kvm ]] || die "/dev/kvm is not accessible"
[[ -r "${fixture}" && -f "${fixture}" ]] || die "fixture is not readable: ${fixture}"
"${docker_cli}" image inspect "${local_image}" >/dev/null 2>&1 || die \
    "local Docker image is unavailable: ${local_image}"

# Create the artifact root before redirecting any command output. Every run is
# retained for review, including failures during image import or VM startup.
artifact_root="${repo}/build/immich-backup-vm-smoke"
mkdir -p -- "${artifact_root}"
source_image_id="$("${docker_cli}" image inspect --format '{{.Id}}' "${local_image}")"
source_image_id="${source_image_id#sha256:}"
target_image_id="$(
    "${container_cli}" image inspect --format '{{.Id}}' "${imported_image}" \
        2>/dev/null || true
)"
target_image_id="${target_image_id#sha256:}"
if [[ "${source_image_id}" != "${target_image_id}" ]]; then
    printf 'Importing exact Docker image %s into rootless containers-storage.\n' \
        "${local_image}"
    "${skopeo_bin}" copy --preserve-digests \
        "docker-daemon:${local_image}" "containers-storage:${imported_image}" \
        >"${artifact_root}/import.log" 2>&1
    target_image_id="$(
        "${container_cli}" image inspect --format '{{.Id}}' "${imported_image}"
    )"
    target_image_id="${target_image_id#sha256:}"
else
    printf 'Rootless containers-storage already has the exact Docker image.\n'
fi
[[ "${source_image_id}" == "${target_image_id}" ]] || die \
    "Docker/Podman image IDs differ: ${source_image_id} != ${target_image_id}"

readonly restic_digest="${restic_image##*@}"
readonly restic_archive="${artifact_root}/restic-amd64.oci.tar"
if ! restic_archive_is_exact "${restic_archive}"; then
    printf 'Caching the pinned amd64 restic fixture from its public registry.\n'
    temporary_archive="${restic_archive}.tmp.$$"
    "${skopeo_bin}" copy --preserve-digests \
        "docker://docker.io/restic/restic@${restic_digest}" \
        "oci-archive:${temporary_archive}:docker.io/restic/restic:0.19.1"
    restic_archive_is_exact "${temporary_archive}" || die \
        "downloaded restic fixture does not match the pinned amd64/Linux image"
    /usr/bin/mv -f -- "${temporary_archive}" "${restic_archive}"
fi

run_dir="$(mktemp -d "${artifact_root}/run.XXXXXX")"
readonly run_dir
readonly scratch_disk="${run_dir}/scratch.raw"
readonly seed_dir="${run_dir}/seed"
readonly bcvk_log="${run_dir}/bcvk.log"
readonly normalized_console="${run_dir}/console.normalized.txt"
readonly vm_name="nas-immich-backup-vm-smoke-${run_dir##*.}"
mkdir -p -- "${seed_dir}"

"${qemu_img}" create -f raw "${scratch_disk}" 2G >"${run_dir}/scratch-create.log"
"${docker_cli}" image inspect "${local_image}" >"${run_dir}/image-metadata.json"
/usr/bin/cp -- "${restic_archive}" "${seed_dir}/restic.oci.tar"
/usr/bin/cp -- "${fixture}" "${seed_dir}/immich-backup-vm-smoke.sh"
/usr/bin/cp -- "${repo}/tests/immich-backup-vm-storage.conf" \
    "${seed_dir}/storage.conf"
/usr/bin/chmod 0555 "${seed_dir}/immich-backup-vm-smoke.sh"

before_scratch_hash="$(sha256sum -- "${scratch_disk}")"
set +e
PATH="${repo}/scripts/bcvk-bin:${PATH}" \
"${timeout_bin}" --foreground --signal=TERM --kill-after=20s 900s \
    "${bcvk_bin}" ephemeral run --rm \
    --network=none --vcpus=2 --memory=4G \
    --output=journal --log-dir="journal,console=${run_dir}" \
    --ro-bind "${seed_dir}:seed" \
    --mount-disk-file "${scratch_disk}:scratch" \
    --karg=selinux=1 --karg=enforcing=0 \
    --karg=systemd.mask=user@.service \
    --karg=systemd.mask=bootc-fetch-apply-updates.timer \
    --karg=systemd.mask=nas-backup-immich.timer \
    --karg=systemd.mask=nas-maintain-immich-backup.timer \
    --karg=systemd.mask=sops-distribute-secrets.service \
    --karg=systemd.mask=nas-krun-network-policy.service \
    --karg=systemd.mask=tailscaled.service \
    --karg=systemd.mask=nftables.service \
    --execute=/run/virtiofs-mnt-seed/immich-backup-vm-smoke.sh \
    --name "${vm_name}" \
    "${imported_image}" \
    >"${bcvk_log}" 2>&1
bcvk_status="$?"
set -e

after_scratch_hash="$(sha256sum -- "${scratch_disk}")"
printf 'scratch_before=%s\nscratch_after=%s\n' \
    "${before_scratch_hash}" "${after_scratch_hash}" >"${run_dir}/scratch-hashes.txt"
tr -d '\r' <"${bcvk_log}" >"${normalized_console}"
sed -n '/NAS_IMMICH_BACKUP_VM_SMOKE_BEGIN/,$p' "${normalized_console}"
if [[ "${bcvk_status}" -ne 0 ]]; then
    printf 'bcvk ephemeral VM failed with status %s; artifacts: %s\n' \
        "${bcvk_status}" "${run_dir}" >&2
    exit "${bcvk_status}"
fi
[[ "${before_scratch_hash}" != "${after_scratch_hash}" ]] || {
    printf 'Guest did not modify the disposable scratch disk; artifacts: %s\n' \
        "${run_dir}" >&2
    exit 1
}
if grep -Fq 'NAS_IMMICH_BACKUP_VM_SMOKE_FAIL' "${normalized_console}"; then
    printf 'Guest reported a failed assertion; artifacts: %s\n' "${run_dir}" >&2
    exit 1
fi
grep -Fq 'NAS_IMMICH_BACKUP_VM_SMOKE_PASS' "${normalized_console}" || {
    printf 'Guest did not report a pass sentinel; artifacts: %s\n' "${run_dir}" >&2
    exit 1
}
printf 'Immich backup VM smoke passed; artifacts retained at %s\n' "${run_dir}"
