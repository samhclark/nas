# Immich deployment and first-use review

## Deployment contract

Immich v3.1.0 runs as four rootless libkrun guests:

| Component | UID | TAP address | Resources | Persistent state |
| --- | ---: | --- | --- | --- |
| server | 51130 | `10.253.10.2` | 4 vCPU, 4 GiB | library, thumbnails, encoded video |
| PostgreSQL | 51140 | `10.253.11.2` | 2 vCPU, 2 GiB | database |
| Valkey | 51150 | `10.253.12.2` | 1 vCPU, 512 MiB | queue/cache data |
| machine learning | 51160 | `10.253.13.2` | 4 vCPU, 4 GiB | rebuildable model/config caches |

All containers follow Immich's rootless UID 1000 contract through generated
`User=1000:1000` settings and matching `keep-id` namespaces. They use the
upstream rootless hardening settings and CPU-only processing; no host GPU is
exposed. PostgreSQL gets the upstream recommended 128 MiB shared-memory
allocation and HDD-oriented configuration.

The PostgreSQL service has an additional compatibility boundary because
libkrun may supply guest root to an entrypoint despite the requested
`User=1000:1000`. Its Quadlet mounts the image-controlled
`/usr/share/nas/immich-database/` adapter tree read-only and uses
that adapter as the entrypoint before the upstream Immich PostgreSQL wrapper.
The adapter has exactly two supported branches: it uses the image's existing
`gosu` to become 1000:1000 when it starts as guest root, and delegates directly
when it already starts as 1000:1000. The upstream wrapper alone is therefore
not the production UID guarantee; the generated UID/GID request and this
adapter are the combined contract.

Only the server is host-published, on loopback TCP 2283. Caddy exposes it as
`https://photos.i.samhclark.com`. PostgreSQL, Valkey, and machine learning have
only generated inter-TAP consumer edges. The database password is encrypted in
SOPS and delivered as separate per-service runtime files.

## Storage and recovery classification

- `tank/immich-server/library` mounted at `/data` is authoritative. It includes
  uploaded originals, the managed library, profiles, and Immich's built-in
  database-backup directory.
- `tank/immich-database/data` is the live PostgreSQL database. It uses
  `recordsize=32K`, LZ4 compression, `atime=off`, and normal ARC data caching.
  This follows current OpenZFS PostgreSQL guidance while retaining PostgreSQL's
  default full-page-write safety and Immich's HDD I/O profile. Its mount is
  private `0700` state, matching PostgreSQL's data-directory initialization.
- `tank/immich-server/thumbs` and `tank/immich-server/encoded-video` are
  regenerable derivatives split out so a future off-site policy can exclude
  them.
- Valkey uses `/var/lib/immich/valkey` on the persistent root filesystem rather
  than a ZFS dataset. The official rootless deployment persists `/data`, but
  Immich's standard deployment treats Valkey as disposable. Keeping the small
  queue/cache directory makes restarts smoother without making it an
  authoritative recovery input.
- Machine-learning state remains persistent for smooth restarts but is not an
  authoritative recovery input.

The host-side backup design protects only the authoritative library dataset,
including its database-backup directory. The separate `thumbs` and
`encoded-video` child datasets are naturally excluded by the nonrecursive ZFS
snapshot, and the live PostgreSQL dataset, Valkey, machine-learning caches, and
bulk media are not backup inputs. See
[`../proposals/application-backups.md`](../proposals/application-backups.md).
The accepted recovery objective, reviewed local and B2 restore sequence, and
isolated-rehearsal evidence are recorded in
[`immich-restore.md`](immich-restore.md).

Backup control-plane provisioning completed on 2026-08-30: the private B2
bucket, `immich/restic/` lifecycle rule, prefix-scoped application key, all five
real SOPS values, and external escrow of the restic encryption password are in
place. The image was deployed and the local restic repository was explicitly
initialized and structurally checked successfully. Its first mirror stopped
before contacting B2 when crun could not start the former passt backend. The
dedicated root-owned TAP correction established guest routing, and the
subsequent explicit MagicDNS override successfully carried a signed request to
B2. That request exposed one remaining prefix-boundary defect:
rclone treated the destination without a trailing slash as the parent object
`immich/restic`, outside the key's intentional `immich/restic/` restriction.
After the directory-shaped correction was deployed, the sync guest wrote the
complete 604-byte empty repository to B2. The immediately following comparison
guest then exited with rclone's temporary-error status before acquiring the TAP
network. The runner now keeps sync and its acceptance comparison sequentially
inside one rclone guest, avoiding a second pass through libkrun's single-shot
100 ms DHCP window. Production validation of that correction completed the
remote comparison itself: rclone found zero differences and two matching
repository objects (`config` and the repository key) beneath
`immich/restic/`. The host runner did not return successfully or record its
remote-success timestamp, so initialization acceptance remains pending. The
first backup attempt then exposed that the capability-free restic guest could
not traverse the private `0750`, UID/GID `51130` snapshot root. The photo-reading
no-network guest now receives only `CAP_DAC_READ_SEARCH`; repository-only
restic guests and the rclone guest remain capability-free. The first backup of
an actual Immich recovery point and the direct-B2 restore rehearsal remain
pending.

## Capacity gate before a large import

The current 500 GB root drive is sufficient for the existing approximately
40.6 GB Immich library and its local encrypted restic repository. It is not the
approved capacity for the planned additional approximately 300 GB import. The
planned 1 TB root-drive replacement is a mandatory precondition for that
import, even if a point-in-time free-space reading appears to fit the source.
The local repository must retain headroom for restic retention and prune work,
and the host alert contract reserves at least 100 GiB and 20% free on `/var`.

Before starting the large import, the operator must complete the drive swap and
confirm both the backup runner's capacity report and the filesystem directly:

```bash
sudo nas-backup-immich status
df -h /var
```

Do not start the import unless `status` reports that the local repository is
healthy and both free-space thresholds remain satisfied after accounting for
the projected library. This check complements, rather than replaces, a recent
successful local backup and B2 mirror.

## Image compatibility preflight

PostgreSQL uses Immich's public-source companion image because it packages the
exact PostgreSQL, pgvector, VectorChord, and tuning contract supported by this
Immich release. Valkey uses the official upstream image. Both remain pinned by
digest and target UID 1000; there is no local companion-image release train.

The database adapter is intentionally a read-only mounted asset rather than a
new `lab-immich-db` image. It keeps the upstream database contents, extensions,
tuning, digest, and security-update path intact while containing the one
demonstrated libkrun identity mismatch. A local database image should be
considered only if a future upstream release cannot be safely handled by this
narrow adapter, requires image changes beyond a read-only asset, creates an
unacceptable provenance gap, or forces repeated image-specific patches. At
that point the project would also need to own a separate digest/update policy,
extension compatibility checks, and security-update release train.

After changing an Immich image digest, image entrypoint, declared or runtime
user, or writable-path/volume contract, run the complete networked, opt-in
preflight before deployment:

```bash
make preflight-immich-images
```

The combined target runs the Podman startup smoke first and the libkrun user
probe second. Either stage failing blocks rollout. The preflight deliberately
remains outside canonical offline `make check` and `make test`.

The first stage, `make smoke-immich-images`, initializes disposable PostgreSQL
state with checksums and requires both PostgreSQL and Valkey to answer under
their declared users. PostgreSQL is started twice: once as 1000:1000 and once
with a 0:0 process identity that emulates libkrun guest-root entry. Both runs
exercise the adapter's respective branch, require readiness and enabled
checksums, and verify that host ownership does not change. It does not prove
that the image behaves the same under libkrun.

The second stage, `make probe-krun-user`, runs the pinned image under libkrun
and classifies the observed entrypoint identity as either guest root or
1000:1000. Either result is supported by the adapter; any other identity is a
compatibility failure that must be investigated before deployment. It does not
exercise PostgreSQL or Valkey readiness, checksums, writable paths, or data
continuity. The probe remains opt-in because it depends on the local libkrun
runtime and networked image access.

## First use

On first successful deployment, open `https://photos.i.samhclark.com` and
create the initial administrator through Immich's setup screen. Afterward,
review Administration settings and confirm the external domain. Do not upload
the only copy of important photos until database backup output and a restore
procedure have been reviewed.

## Operator verification

Agents do not run these commands on the NAS. After the scheduled image update,
the operator can run `nas-diagnose-immich` for a comprehensive, secret-safe,
read-only report, or use this short verification:

```bash
( cd / &&
  for spec in \
    '_nas_immichserver 51130 immich-server.service' \
    '_nas_immichdatabase 51140 immich-database.service' \
    '_nas_immichvalkey 51150 immich-valkey.service' \
    '_nas_immichmachinelearning 51160 immich-machine-learning.service'; do
    read -r user uid unit <<<"${spec}"
    sudo systemctl is-active "user@${uid}.service"
    sudo runuser -u "${user}" -- env \
      HOME="/var/home/${user}" \
      XDG_RUNTIME_DIR="/run/user/${uid}" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
      systemctl --user is-active "${unit}"
  done
)
curl --fail --silent --show-error \
  http://127.0.0.1:2283/api/server/ping
curl --fail --silent --show-error \
  https://photos.i.samhclark.com/api/server/ping
```

Both HTTP requests should return a JSON pong response. If they do not, inspect
only the affected user unit before changing state, for example:

```bash
sudo journalctl --unit user@51130.service --boot --lines 100 --no-pager
```

For the database, also inspect the service journal for the adapter's identity
diagnostic. A healthy deployment should report either that it transitioned
guest root to 1000:1000 or that it was already running as 1000:1000. The
database user unit should remain active, TCP 5432 should answer through the
generated `immich-database.krun` endpoint, and the restart counter should stop
increasing. Confirm that `/var/lib/immich/database` remains mode `0700` and
owned by `_nas_immichdatabase`; the adapter must not repair ownership from
inside the rootless guest.

If `nas-prepare-immich-database-storage.service` requests an explicit repair,
first confirm `immich-database.service` is stopped and inspect the reported
ownership, mode, and labels. To authorize the bounded reconciliation without
deleting or reinitializing PostgreSQL, create
`/var/lib/nas-repairs/immich-database/repair-required` and restart the storage
preparation unit. The runtime refuses mutation while the container is running
and consumes the marker only after all postconditions pass. Then restart the
database user unit followed by the server user unit.

The review step is read-only:

```bash
( cd / &&
  sudo runuser -u _nas_immichdatabase -- env \
    HOME=/var/home/_nas_immichdatabase \
    XDG_RUNTIME_DIR=/run/user/51140 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/51140/bus \
    systemctl --user is-active immich-database.service
)
sudo systemctl status nas-prepare-immich-database-storage.service --no-pager
sudo stat -c '%U:%G %a %C %n' /var/lib/immich/database
```

Only after confirming the database service is not active, this state-changing
sequence authorizes one bounded repair and retries the two affected services:

```bash
( cd / &&
  sudo install -d -m 0755 -o root -g root \
    /var/lib/nas-repairs/immich-database &&
  sudo install -m 0600 -o root -g root /dev/null \
    /var/lib/nas-repairs/immich-database/repair-required &&
  sudo systemctl restart nas-prepare-immich-database-storage.service &&
  sudo test -r /run/nas-storage/immich-database/ready &&
  sudo runuser -u _nas_immichdatabase -- env \
    HOME=/var/home/_nas_immichdatabase \
    XDG_RUNTIME_DIR=/run/user/51140 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/51140/bus \
    systemctl --user restart immich-database.service &&
  sudo runuser -u _nas_immichserver -- env \
    HOME=/var/home/_nas_immichserver \
    XDG_RUNTIME_DIR=/run/user/51130 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/51130/bus \
    systemctl --user restart immich-server.service
)
```
