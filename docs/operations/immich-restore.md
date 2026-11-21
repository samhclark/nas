# Immich recovery runbook

Use this procedure to recover Immich from Backblaze B2 on a replacement
machine or rehearse that recovery in a disposable VM. The
[November 21 laptop rehearsal](#laptop-rehearsal-on-2026-11-21) verified the
direct B2 restore, database import, original files, and browser access. It
used the laptop's SOPS age identity. The separately escrowed read-only
credential still needs an end-to-end test.

[Find a backup](#1-select-and-size-a-recovery-point) ·
[Restore the library](#2-restore-the-library-in-the-guest) ·
[Prepare Immich](#3-prepare-a-matching-offline-immich) ·
[Import and validate](#4-import-postgresql-and-check-every-original) ·
[Test and clean up](#5-verify-the-browser-and-finish)

## 1. Select and size a recovery point

Record the start time with `date -u +%FT%TZ`. For a rehearsal, treat this as
the time the NAS was lost. Do the B2 reads while the daily mirror and weekly
maintenance are idle. The recovery objective is 24 hours of database changes.
The recovery point pairs a gzip-valid database dump with a filesystem snapshot
taken after that dump.

On the laptop, install restic and retrieve the bucket name, Backblaze S3
endpoint, restic password, and B2 key from the external password manager.
Use a key restricted to reading and listing `immich/restic/` in the backup
bucket. Keep a copy of this runbook with the recovery material. The endpoint
is the bucket's S3 hostname, such as `s3.us-east-005.backblazeb2.com`.
Load these functions into a fresh Bash session without shell tracing.
Use the same functions later in the guest.

```bash
configure_b2() {
    local endpoint=${1#https://} region
    endpoint=${endpoint%/}
    [[ "$endpoint" =~ ^s3\.([a-z0-9-]+)\.backblazeb2\.com$ ]] || return 1
    region="${BASH_REMATCH[1]}"
    [[ "$2" =~ ^[A-Za-z0-9-]+$ ]] || return 1
    unset RESTIC_REPOSITORY_FILE
    export AWS_DEFAULT_REGION="$region"
    export RESTIC_REPOSITORY="s3:https://$endpoint/$2/immich/restic"
}

b2_restic() {
    restic --no-lock -o "s3.region=${AWS_DEFAULT_REGION:?}" \
        -o s3.bucket-lookup=path "$@"
}

load_b2_prompt() {
    local endpoint bucket
    unset AWS_SESSION_TOKEN RESTIC_PASSWORD_FILE RESTIC_PASSWORD_COMMAND
    read -rp 'B2 S3 endpoint: ' endpoint || return
    read -rp 'B2 bucket name: ' bucket || return
    configure_b2 "$endpoint" "$bucket" || return
    read -rsp 'Read-only B2 key ID: ' AWS_ACCESS_KEY_ID || return
    printf '\n'
    read -rsp 'Read-only B2 application key: ' AWS_SECRET_ACCESS_KEY || return
    printf '\n'
    read -rsp 'Restic repository password: ' RESTIC_PASSWORD || return
    printf '\n'
    [[ -n "$AWS_ACCESS_KEY_ID" && -n "$AWS_SECRET_ACCESS_KEY" && -n "$RESTIC_PASSWORD" ]] || return 1
    export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY RESTIC_PASSWORD
}

(
    set +x
    set -euo pipefail
    load_b2_prompt
    b2_restic snapshots --host nas --tag immich --path /source
    read -rp 'Newest matching snapshot at or before drill start: ' snapshot
    [[ "$snapshot" =~ ^[a-f0-9]{8,64}$ ]]
    b2_restic stats --mode restore-size "$snapshot"
)
```

Record the snapshot ID, timestamp, and restore size. Compare it with free
space on the encrypted storage backing the VM, allowing room for Linux,
container images, PostgreSQL, and laptop use. Restic sizing can take several
minutes. This is a safe stopping point for a later session. Resume with the
same snapshot ID and drill start time.

If access fails, check the endpoint, bucket, key scope, and restic password.
If the listing is empty or implausibly small, inspect the repository
inventory before proceeding. An optional read-only rclone inventory should
show `config`, `data`, `index`, `keys`, and `snapshots` under the single
`immich/restic/` prefix. Preserve the evidence if `restic check` fails and
review hidden B2 object versions before recovering to a separate destination.
Never initialize, prune, repair, or sync into the B2 repository as part of
recovery.

### SOPS fallback

If the external credential is unavailable, the laptop's existing SOPS age
identity can unlock the five values in the repository. This uses the
replication key, which has write permissions. Use only the read commands in
this runbook with `--no-lock`. Keep the age key on the trusted laptop, and
record the credential fallback in the drill evidence.

Run this from the repository root on the laptop, with the preceding
functions already loaded. It passes the five selected fields in memory.

```bash
sops_b2_fields() {
    sops decrypt --output-type json overlay-root/usr/share/nas/secrets/secrets.sops.yaml |
        jq -ej '[
            ."immich-backup-b2-s3-endpoint",
            ."immich-backup-b2-bucket",
            ."immich-backup-b2-key-id",
            ."immich-backup-b2-application-key",
            ."immich-backup-restic-password"
        ] | if all(.[]; type == "string" and length > 0)
            then .[] + "\u0000" else error("missing Immich recovery field") end'
}

load_b2_stream() {
    local -a fields=()
    mapfile -d '' -t fields
    [[ ${#fields[@]} -eq 5 ]] || return 1
    unset AWS_SESSION_TOKEN RESTIC_PASSWORD_FILE RESTIC_PASSWORD_COMMAND
    configure_b2 "${fields[0]}" "${fields[1]}" || return
    export AWS_ACCESS_KEY_ID="${fields[2]}" AWS_SECRET_ACCESS_KEY="${fields[3]}"
    export RESTIC_PASSWORD="${fields[4]}"
}

(
    set +x
    set -euo pipefail
    sops_b2_fields | (
        load_b2_stream
        b2_restic snapshots --host nas --tag immich --path /source
    )
)
```

To size the selected point with the fallback, replace the last `snapshots`
command with `b2_restic stats --mode restore-size SNAPSHOT_ID`. Preserve the
pipe into `load_b2_stream` so decrypted values stay out of terminal output.

## 2. Restore the library in the guest

Boot a disposable Linux VM with a browser, Podman, restic, ZFS and SELinux
tools, and Python 3. Use an OS disk and a second virtual disk backed by
encrypted laptop storage. Leave host shares, physical disk passthrough, and
NAS mounts out of the guest. Verify that `/var` is writable, SELinux is
enforcing, and ZFS loads before entering credentials. Use the guest's normal
installation path. The rehearsal's bootc-generated CoreOS disk required
manual boot and writable `/var` setup.

Inside the VM, open a root Bash shell. Identify the blank scratch disk by
size and stable virtual-disk ID with `lsblk -o NAME,TYPE,SIZE,FSTYPE,SERIAL,MOUNTPOINTS`.
Substitute its actual ID in this example. The pool creation writes to that
disk. Confirm the target and the empty dataset before proceeding.

```bash
disk=/dev/disk/by-id/virtio-immich-scratch
if [[ "$(getenforce)" == Enforcing && -w /var && -b "$disk" ]] &&
   [[ "$(lsblk -ndo TYPE "$disk")" == disk && -z "$(wipefs -n "$disk")" ]]; then
    zpool create -o ashift=12 -O mountpoint=none -O compression=lz4 \
        -O atime=off -O xattr=sa -O acltype=posixacl immichdrill "$disk" &&
    zfs create -o mountpoint=/mnt/immich-drill immichdrill/recovery &&
    findmnt -T /mnt/immich-drill
else
    printf 'Check the guest prerequisites and the blank scratch disk ID.\n' >&2
fi
```

Load `configure_b2`, `b2_restic`, and `load_b2_prompt` above in the root
shell. Define the following function. It checks the destination again and
restores numeric ownership and xattrs as root. The restore leaves the
authoritative library at `/mnt/immich-drill/source`.

```bash
restore_b2() (
    set -euo pipefail
    target=/mnt/immich-drill
    [[ "${1:-}" =~ ^[a-f0-9]{8,64}$ ]]
    [[ $EUID -eq 0 && "$(getenforce)" == Enforcing ]]
    [[ "$(findmnt -no SOURCE -T "$target")" == immichdrill/recovery ]]
    [[ -z "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit)" ]]
    b2_restic check
    b2_restic restore "${1:?snapshot ID required}" --target "$target" --verify
)

(
    set +x
    set -euo pipefail
    load_b2_prompt
    read -rp 'Saved snapshot ID: ' snapshot
    restore_b2 "$snapshot"
)
```

For the SOPS fallback, define `restore_b2` on the trusted laptop as well.
Use an authenticated SSH connection to the disposable guest through a
temporary laptop-loopback forward. The example assumes a Bash login shell
and guest-only `sudo -n` authorization. Substitute the reviewed SSH port,
guest login, and saved snapshot ID. The credential stream goes to the
restore process and ends with it.

```bash
(
    set +x
    set -euo pipefail
    snapshot=SNAPSHOT_ID
    [[ "$snapshot" =~ ^[a-f0-9]{8,64}$ ]]
    guest_script="$(declare -f configure_b2 b2_restic load_b2_stream restore_b2)
set +x
set -euo pipefail
load_b2_stream
restore_b2 \"\$1\""
    printf -v remote 'sudo -n bash -c %q bash %q' "$guest_script" "$snapshot"
    sops_b2_fields | ssh -T -p 22783 drill@127.0.0.1 "$remote"
)
```

Continue in the guest root shell after both restic commands succeed.

## 3. Prepare a matching offline Immich

Select the newest scheduled dump and check that it is readable.

```bash
shopt -s nullglob
dumps=(/mnt/immich-drill/source/backups/immich-db-backup-*.sql.gz)
unset DUMP
if (( ${#dumps[@]} )); then
    dump_candidate=${dumps[-1]}
    if gzip -t -- "$dump_candidate"; then
        DUMP=$dump_candidate
        stat -c '%n %s bytes, modified %y' "$DUMP"
    fi
fi
test -n "${DUMP:-}"
```

Record the Immich and PostgreSQL versions from the dump name. Interpret
its timestamp in the Immich server's configured timezone
(`America/Chicago` for this NAS). Confirm the dump precedes the snapshot and
compute its age at drill start. Record an age over 24 hours as a freshness
failure even if the application restores.

Set `SERVER_IMAGE`, `POSTGRES_IMAGE`, and `VALKEY_IMAGE` to compatible
digest-pinned images. Match the Immich server version and the PostgreSQL
major recorded in the dump filename. Use the matching deployment revision's
[server](../../quadlets/immich-server.toml),
[database](../../quadlets/immich-database.toml), and
[Valkey](../../quadlets/immich-valkey.toml) image references. The commands
below use the PostgreSQL 18 image's `/var/lib/postgresql` volume path. Check
the image's data directory if using a different major. Pull the three images
while the guest is online.

```bash
(
    set -e
    for image in "${SERVER_IMAGE:?}" "${POSTGRES_IMAGE:?}" "${VALKEY_IMAGE:?}"; do
        podman pull "$image"
    done
)
```

Confirm all pulls succeeded. Close the recovery credential shells, remove
any temporary SSH forwarding, and disconnect the VM's external adapter.
Inside the guest, `ip -4 route show default` and `ip -6 route show default`
should both return empty output. Keep the private container network and
open the browser through the VM console.

Restore the library root's owner and SELinux policy. Preserve restic's
descendant ownership. A sampled descendant should use IDs from the service
identity or its subordinate range (`51130` or `511300000`–`511365535`) and
report `object_r:container_file_t:s0`. On Fedora CoreOS, use `/mnt` in the
fcontext rule even though that path resolves to `/var/mnt`.

```bash
LIBRARY=/mnt/immich-drill/source
SCRATCH=/var/lib/immich-drill
chown 51130:51130 "$LIBRARY"
semanage fcontext -a -r s0 -t container_file_t "$LIBRARY(/.*)?"
restorecon -F -R -x "$LIBRARY"
for folder in thumbs encoded-video; do
    install -d -m 0750 -o 51130 -g 51130 "$SCRATCH/$folder"
    install -m 0644 -o 51130 -g 51130 /dev/null "$SCRATCH/$folder/.immich"
    semanage fcontext -a -r s0 -t container_file_t "$SCRATCH/$folder(/.*)?"
    restorecon -F -R -x "$SCRATCH/$folder"
done
stat -c '%u:%g %a %C' "$LIBRARY" "$SCRATCH"/{thumbs,encoded-video}/.immich
```

The backup contains originals, profiles, and scheduled SQL dumps. Immich can
regenerate thumbnails and encoded videos. The scratch mounts supply the
required directories and markers for those excluded trees. Valkey starts
empty, and machine learning is unnecessary for acceptance.

## 4. Import PostgreSQL and check every original

Run these commands only in the offline guest. Start with a fresh volume,
create a disposable database secret, and start PostgreSQL alone.

```bash
set -o pipefail
podman network create immich-drill
podman volume create immich-drill-postgres
openssl rand -hex 32 | podman secret create immich-drill-db-password -
podman run -d --name immich-drill-postgres --network immich-drill --pull=never \
    --secret immich-drill-db-password \
    -e POSTGRES_PASSWORD_FILE=/run/secrets/immich-drill-db-password \
    -e POSTGRES_USER=postgres -e POSTGRES_DB=immich \
    -e POSTGRES_INITDB_ARGS=--data-checksums \
    -v immich-drill-postgres:/var/lib/postgresql "$POSTGRES_IMAGE"
podman exec immich-drill-postgres pg_isready -h 127.0.0.1 -U postgres -d immich
```

Repeat `pg_isready` until it accepts connections. Import the dump in one
transaction with Immich's search path adjustment. Stop on any pipeline
failure. Use fresh database storage for a retry if the application has
already run against it.
[Immich command-line restore instructions](https://docs.immich.app/administration/backup-and-restore/#restore-via-command-line)

```bash
gzip -dc -- "${DUMP:?set DUMP to the verified SQL dump}" |
    sed "s/SELECT pg_catalog.set_config('search_path', '', false);/SELECT pg_catalog.set_config('search_path', 'public, pg_catalog', true);/g" |
    podman exec -i immich-drill-postgres psql -X -U postgres -d immich \
        --single-transaction --set ON_ERROR_STOP=on
```

Check the PostgreSQL major, nonzero user and active-asset counts, and album
count. These queries apply to the Immich v3 schema used in the rehearsal.
They print counts and versions only. A zero count in either original
directory is valid if the library uses the other directory.

```bash
db() {
    podman exec immich-drill-postgres psql -X -U postgres -d immich -At \
        --set ON_ERROR_STOP=on "$@"
}
db -c 'SELECT current_setting('\''server_version'\''),
    (SELECT count(*) FROM public."user"),
    (SELECT count(*) FROM public.album),
    (SELECT count(*) FROM public.asset WHERE "deletedAt" IS NULL)'
db -c 'SELECT row_to_json(paths) FROM
    (SELECT "originalPath" AS path FROM public.asset WHERE "deletedAt" IS NULL) paths' |
    python3 -c '
import json
from pathlib import Path, PurePosixPath
import sys

root = Path(sys.argv[1]).resolve()
counts = dict(upload=0, library=0, generated_video=0, missing=0, unexpected=0)
for row in sys.stdin:
    value = json.loads(row)["path"]
    parts = PurePosixPath(value).parts if isinstance(value, str) else ()
    if parts[:2] != ("/", "data") or len(parts) < 4 or ".." in parts:
        counts["unexpected"] += 1
        continue
    folder = parts[2]
    if folder in ("upload", "library"):
        counts[folder] += 1
        candidate = root.joinpath(*parts[2:])
        if not candidate.resolve().is_relative_to(root) or not candidate.is_file():
            counts["missing"] += 1
    elif folder == "encoded-video":
        counts["generated_video"] += 1
    else:
        counts["unexpected"] += 1
print(json.dumps(counts))
sys.exit(bool(counts["missing"] or counts["unexpected"] or not (counts["upload"] + counts["library"])))
' "$LIBRARY"
```

Require zero missing originals and zero unexpected path prefixes before
starting the server. The generated-video count covers Immich v3 records under
`/data/encoded-video/`. In an actual recovery, investigate discrepancies
before exposing the service or replacing existing data.

## 5. Verify the browser and finish

Start Valkey and Immich on the private Podman network. Keep the application
bound to guest loopback. Run only the API worker for the drill.

```bash
podman run -d --name immich-drill-valkey --network immich-drill --pull=never \
    "$VALKEY_IMAGE"
podman run -d --name immich-drill-server --network immich-drill --pull=never \
    -p 127.0.0.1:2283:2283 --secret immich-drill-db-password \
    -e DB_HOSTNAME=immich-drill-postgres -e DB_USERNAME=postgres -e DB_DATABASE_NAME=immich \
    -e DB_PASSWORD_FILE=/run/secrets/immich-drill-db-password \
    -e REDIS_HOSTNAME=immich-drill-valkey -e IMMICH_WORKERS_INCLUDE=api \
    -v "$LIBRARY:/data:rw" -v "$SCRATCH/thumbs:/data/thumbs:rw" \
    -v "$SCRATCH/encoded-video:/data/encoded-video:rw" "$SERVER_IMAGE"
curl -fsS --retry 45 --retry-connrefused --retry-delay 2 --retry-max-time 90 \
    http://127.0.0.1:2283/api/server/ping
curl -fsS http://127.0.0.1:2283/api/server/version
```

In the guest browser, open `http://127.0.0.1:2283` and sign in with the Immich
email and password valid when the dump was taken. Open an older and a recent
original, download an original, and verify dates and album membership. JPEGs
are suitable for the viewing checks if HEIC is hard to render. Empty
thumbnails are expected. Keep the guest offline and leave derivative jobs
paused during a rehearsal.

Record the credential source, repository and snapshot IDs, dump age, restored
size, versions, elapsed time, counts, and browser outcomes without personal
filenames or secrets. Record any failed checks and manual repairs. This is the
off-site restore evidence for R2. Testing external recovery-material escrow
remains part of R3.

For a rehearsal, stop the disposable containers and power off the VM.
Confirm the exact image and credential paths before discarding them. From the
laptop, recheck the production Immich UI and `/api/server/ping` after
teardown. Agents must never SSH to or run commands on the production NAS.

For a real recovery, retain the verified staged copy. Install the matching
service configuration and [runtime secrets](../architecture/secrets.md) on
the replacement host and restore ownership and persistent SELinux labels.
Run the original-file check against the final mount before opening ingress.
Regenerate derivatives once the service is available. For a local rehearsal
on the NAS, the operator can pair a reviewed dump with a later writable ZFS
clone in a uniquely named disposable dataset, then follow steps 3–5 with
paths adjusted for that copy. Use NAS loopback and an operator-managed
SSH tunnel for browser access. Keep production datasets outside the
rehearsal's writable mounts.

## Rehearsal evidence: 2026-08-29

The first isolated rehearsal used:

- Immich `v3.1.0`, pinned server digest
  `sha256:b434cb9287eea1471c9974845914d4dd328c9c2d652e446ed4930f99944f0ceb`;
- PostgreSQL image
  `18-vectorchord1.1.1-pgvector0.8.5`, pinned digest
  `sha256:4303394b926b7b7af5d4d15a6372c3cfff4ee994a4cf32c5479aa5ef73972077`,
  reporting PostgreSQL `18.4` after restore; and
- Valkey `9.1.1`, pinned digest
  `sha256:70739f85ad2ee01a726a965584a0f94895f01b0c60b3cc8b0aeef11eaa6888cf`.

Immich retained 14 daily scheduled dumps plus one restore point. Every
`.sql.gz` file passed gzip validation. The selected database point was
`immich-db-backup-20260829T020000-v3.1.0-pg18.4.sql.gz`, 45,370,868 bytes,
paired with
`tank/immich-server/library@immich-r1-20260829T020000`, created later that day.

The dump restored transactionally into fresh storage. It contained one user,
12,154 total asset records, 12,153 active asset records, and 16 albums. All
11,810 authoritative source records resolved to files in the disposable
library clone. The remaining 343 active records were hidden generated-video
records below `/data/encoded-video`; all were absent as intended by policy.
There were no unexpected path prefixes.

The isolated server passed its ping and version endpoints. The existing user
could authenticate, representative photos were viewable, an original could be
downloaded, and restored dates and album membership were correct. Missing
thumbnail requests produced expected `ENOENT` errors because the rehearsal
excluded generated thumbnails.

Two operational details were discovered and are now part of the reviewed
sequence: excluded generated directories still require `.immich` markers, and
new marker files below a clone mountpoint outside the production fcontext rules
must be labeled `container_file_t:s0` rather than their default `var_lib_t`.

## Laptop rehearsal on 2026-11-21

The drill started at 21:03:30 UTC. It selected the 10:00:03 UTC B2 snapshot
`9bc981de5cbbeaedb0b8b6d4b1ef60d169ddce41c00e4c6cad9d6ef4f58386a6`.
The restic repository ID was
`4d4d3ce26d88385a73eda368d336c957d489217da7d1dbbc8bee07a1efae8116`.
Restic 0.20.0 sized it at 44,388,448,139 bytes (41.3 GiB). Restic 0.19.1
checked the repository with no errors, then restored and verified the snapshot
directly from B2 into `immichdrill/recovery` in a disposable laptop VM with
ZFS and SELinux enforcing.

The password-manager read-only-key attempt ended before snapshot selection.
The laptop decrypted the repository's five Immich backup values with its
local SOPS age identity and passed them to the recovery process in memory.
All B2 commands used `--no-lock` and read the repository. The laptop and B2
provided all recovery inputs. The CoreOS guest needed manual boot and writable
`/var` setup before the restore.

The latest of 14 retained dumps was
`immich-db-backup-20261121T020000-v3.4.2-pg18.4.sql.gz`. Its 47,331,247 bytes
passed gzip verification. The dump was taken at 08:00:00 UTC, 13 hours,
3 minutes, and 30 seconds before the drill start. The later filesystem
snapshot and the dump met the 24-hour database recovery objective.

The VM's external adapter was disconnected before the transactional import
into PostgreSQL 18.4. The database contained one user, 16 albums, and 12,391
active assets. All 12,048 authoritative originals resolved to files under
`/data/upload/`, with zero originals under `/data/library/`, zero missing
originals, and zero unexpected prefixes. The remaining 343 active records
referenced generated videos.

Immich 3.4.2, PostgreSQL 18.4 with VectorChord, and Valkey 9.1.3 ran on a
private guest network. The first successful Immich API response was at
22:17:58 UTC, about 74 minutes after the drill started. Firefox opened the
guest loopback URL. The operator signed in and accepted the older and recent
original, date, and album checks. Three nonempty downloads were present in
the guest. Timeline thumbnails were absent as expected. The operator noted
possible HEIC display trouble, and HEIC rendering was unverified.

The rehearsal containers, private network, database volume, and disposable
database secret were removed. A manual scratch-pool export reported an
unmount-path error. The guest then powered off and the laptop's rehearsal
disk images and temporary SSH credentials were removed. The normal
production UI returned HTTP 200 and its API returned `pong` after teardown.
