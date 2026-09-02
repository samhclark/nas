# Application backup and replication design

Status: implemented, provisioned, and deployed for Immich as of 2026-08-30.
The local repository was initialized and structurally checked successfully;
the first B2 mirror exposed the rejected passt networking path. The deployed
root-owned TAP and explicit guest-DNS corrections now carry requests to B2.
That validation exposed a final prefix-boundary defect: rclone probed the
parent object `immich/restic` while the application key correctly permits only
`immich/restic/`. After the directory-shaped remote-root correction was
deployed, production confirmed that the sync guest wrote the complete 604-byte
empty repository, but the immediately following comparison guest missed
libkrun's single-shot 100 ms DHCP response window and returned a temporary
error. Sync and its acceptance comparison now run sequentially in one rclone
guest. Production validation completed the initial mirror with zero
differences and two matching repository objects, but the host runner did not
return successfully or record remote success. The first backup attempt exposed
that dropping every capability prevents guest root from traversing Immich's
private `0750`, UID/GID `51130` snapshot root. The no-network photo-reading
guest now receives only `CAP_DAC_READ_SEARCH`; repository-only restic guests
and the rclone guest remain capability-free. The next production attempt could
traverse the snapshot root, but restic's device filter treated all descendants
across the virtiofs boundary as separate filesystems and saved zero files from
the measured 40,323,811,505-byte source. The unusable snapshot passed a
structural check and its five repository objects matched B2; remote success
still was not recorded because the runner both raced its asynchronous rclone
log writer and parsed mixed plain-text/JSON output as pure JSON. Logging is now
synchronized before parsing while preserving both the VM and logger statuses,
and metric extraction tolerates non-JSON lines. The device filter is removed.
A post-backup content gate selects the expected host/path/tag, requires at
least one file and 99% of the measured regular-file bytes, and forgets a
rejected snapshot before failing. Maintenance validates the newest matching
snapshot before pruning or replication. Initialization acceptance, the first
actual recovery point, and restore validation remain pending. The implementation
remains deliberately application-specific; this document does not define a
generic backup schema or authorize arbitrary backup hooks in service TOML.

## Recovery boundary

Backups describe a recoverable application, not merely a list of directories.
Immich accepts a 24-hour off-site RPO. Its recovery point is the newest
validated database dump plus a later point of the authoritative filesystem:

- `tank/immich-server/library`, including uploaded originals, managed-library
  originals, profiles, and Immich's database-backup output, is included;
- the child `thumbs` and `encoded-video` datasets, live PostgreSQL storage,
  Valkey state, machine-learning caches, and bulk media are excluded; and
- the database dump must be nonempty, gzip-valid, unchanged while validated,
  and less than 26 hours old before a filesystem snapshot is taken.

The daily runner creates a uniquely named, nonrecursive ZFS snapshot of the
library after validating the dump. That ordering may retain unreferenced files,
but must not select a database point that refers to authoritative files absent
from the filesystem point. The runner always destroys its current staging
snapshot. On later runs it reports and removes only snapshots with the exact
backup-owned prefix that are older than 48 hours and have neither holds nor
clones.

## Local repository and isolation

The local repository is `/var/lib/nas-backups/immich/restic`. A pinned restic
0.19.1 image runs as a short-lived root-managed libkrun guest with no network,
a read-only snapshot mount, the repository read-write, and only the restic
password credential. The guest has 2 vCPUs, 2 GiB RAM, reduced CPU and I/O
priority, and a read-only root filesystem. All capabilities are dropped by
default; only the photo-reading backup invocation re-adds
`CAP_DAC_READ_SEARCH` so guest root can traverse the private read-only snapshot.
Every nonzero restic exit status, including partial-backup status 3, fails the
run. The nonrecursive ZFS snapshot, rather than a guest device-number filter,
defines the dataset boundary. After backup, the runner verifies that the newest
matching restic snapshot processed at least one file and at least 99% of the
regular-file bytes measured immediately before snapshot creation. An
incomplete recovery point is forgotten locally and fails before the structural
check, success timestamp, or replication. Maintenance also rejects an invalid newest
matching snapshot before retention or replication. Source measurement prunes
the visible `.zfs` namespace, and a valid run removes historical zero-content
snapshots before local verification and replication.

Restic provides encryption and authentication before any repository object is
eligible for replication. A local structural check is a hard gate: failed
verification preserves the local recovery point but prevents all replication.
Retention keeps 14 daily, 8 weekly, 12 monthly, and 3 yearly snapshots.
`forget --prune` runs weekly, followed by another local check before the
changed repository is mirrored. See restic's
[encryption design](https://restic.readthedocs.io/en/stable/070_encryption.html)
for the repository confidentiality and authentication boundary.

The current 500 GB root drive is sufficient for today's approximately 40.6 GB
library. The planned 1 TB root-drive replacement is mandatory before importing
the additional approximately 300 GB library; see
[`../operations/immich.md`](../operations/immich.md).

## B2 replication boundary

One private NAS-backups B2 bucket may contain separate application prefixes,
but every application has an independent repository, credential, schedule, and
recovery procedure. Immich owns `immich/restic/`. Future Paperless work must
use its own prefix and must not broaden the Immich credential.

A pinned rclone 1.74.0 image runs in a separate libkrun guest on the dedicated
root-owned `krun-backup` TAP (`10.253.19.2/30`). The generated host policy gives
that TAP DHCP, anti-spoofing, outbound NAT, and no inbound, host, or inter-TAP
access. The guest receives the encrypted local repository read-only plus only
the Immich B2 credential, bucket, and S3 endpoint. It never receives the restic
password or the photo-library mount. It synchronizes the complete repository,
excluding restic lock files, with checksum comparison, `--delete-after`, and a
5 MiB/s upload limit. Local and B2 repository bytes are compared after every sync.
Failure leaves the local backup intact and does not advance the remote-success
timestamp. The ordering follows the documented
[rclone sync behavior](https://rclone.org/commands/rclone_sync/): new objects
arrive before deletions are applied.

The operator provisions the private bucket and an Immich key limited to the
bucket prefix and the required read, write, and delete capabilities. The
operator also creates a prefix lifecycle rule with
`daysFromHidingToDeleting: 30` and no upload-age hiding. This preserves hidden
versions for deletion recovery without expiring current restic objects. Bucket
creation, application-key creation, and lifecycle configuration are manual B2
control-plane actions and are never performed automatically by the NAS image.
See Backblaze's
[lifecycle-rule documentation](https://www.backblaze.com/docs/en/cloud-storage-lifecycle-rules).

## Secrets and operator interface

The SOPS source contains five host-only values, distributed as root-owned
runtime files:

- `immich-backup-restic-password`
- `immich-backup-b2-key-id`
- `immich-backup-b2-application-key`
- `immich-backup-b2-bucket`
- `immich-backup-b2-s3-endpoint`

All five real values were added through the existing SOPS workflow on
2026-08-30. The generated restic password was copied to the administrator's
external password manager. R3 still owns verification that the complete
recovery set—including bucket identity and endpoint, the scoped B2 recovery
credential, and minimum fresh-host recovery instructions—is independently
available. Neither generated defaults nor placeholder credentials are
acceptable for deployment.

The stable operator entry points are:

- `nas-backup-immich init` explicitly initializes an empty local repository
  and performs the first mirror. If that mirror is interrupted, the same
  explicit command may resume only after authenticating the runner-checked
  repository and proving it still contains zero snapshots. Access failures
  never reinitialize or replace it.
- `nas-backup-immich run` performs the daily dump validation, snapshot, local
  backup and check, and B2 mirror under one lock.
- `nas-backup-immich maintain` performs retention, integrity checks, and
  replication under the same lock.
- `nas-backup-immich status` reports capacity, latest local and remote success,
  repository size, snapshots, and stale staging snapshots without secrets.

The daily timer runs at 04:00 America/Chicago, after Immich's 02:00 database
dump. Deployment follows the normal main-branch image flow. B2 and SOPS
provisioning completed on 2026-08-30. Local repository initialization and its
first structural check also completed that day. The initial empty-repository
mirror and byte comparison subsequently completed with zero differences. The
runner did not record remote success, so initialization acceptance remains
pending. The operator deploys the private-source traversal correction, retries
initialization and the first backup run, and returns secret-safe output from
reviewed commands.

## Verification and completion

Atomic node-exporter textfile metrics cover local and remote last success,
current run result, integrity-check success, source and repository sizes,
duration, and transferred bytes. Alerts fire when local or remote success is
missing or older than 36 hours, a completed run fails, integrity verification
is older than 10 days, or `/var` has less than 100 GiB or 20% free.

Local and remote structural checks run weekly. The remote check rotates
`--read-data-subset=1/12` so all B2 pack data is cryptographically read over
twelve weeks. These checks complement the byte comparison after each mirror;
none substitutes for an application-level restore.

R2 remains incomplete until the first B2 backup is restored from B2 into a
disposable ZFS dataset and fresh application state. Completion requires the
database-to-authoritative-file check, representative UI and original-download
checks, teardown of disposable resources, and production-health revalidation.
The authoritative procedure is
[`../operations/immich-restore.md`](../operations/immich-restore.md).

Only after Paperless supplies a second concrete consistency and recovery model
should the project generalize an application backup schema. VM-launching and
metric-writing primitives may be shared without pretending that applications
have interchangeable recovery semantics.
