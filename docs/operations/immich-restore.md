# Immich recovery runbook

## Scope

This is the authoritative Immich restore runbook. It covers local and Backblaze
B2 recovery-point selection, retrieval and decryption, restoration into
isolation, application-level validation, and safe teardown. Remote repository
access does not replace the recovery-unit, restore, or acceptance contracts
proved by the first local rehearsal.

## Recovery objective

The accepted Immich recovery-point objective is 24 hours of database changes.
The current daily automatic database dump therefore satisfies the recovery
frequency requirement, provided monitoring or later backup automation proves
that dumps continue to be created and retained.

A recoverable Immich point consists of:

- one gzip-valid Immich database dump from `/data/backups`; and
- the authoritative filesystem state from
  `tank/immich-server/library`, captured no earlier than that dump.

Take the database dump first and the filesystem copy or snapshot second. This
ordering can leave unreferenced files in the filesystem, but it must not leave
the restored database referring to absent authoritative source files. Keep the
gap as short as practical. Changes committed to Immich after the database dump
are outside that recovery point even if their files appear in the later
filesystem copy.

The recoverable unit includes uploaded originals, managed-library originals,
profiles, and the database-backup output. It excludes `thumbs`,
`encoded-video`, Valkey state, and machine-learning state because those are
regenerable derivatives or caches. Immich v3 can retain hidden `asset` records
whose `originalPath` is below `/data/encoded-video`; validation must classify
those as generated data rather than missing authoritative originals.

## B2 recovery prerequisites

The administrator's external password manager, rather than the NAS alone,
must contain the restic repository password, Immich-scoped B2 key ID and
application key, bucket name, S3 endpoint, and this runbook. Recovery also
requires pinned or version-compatible `restic` and `rclone` binaries and enough
disposable capacity for the selected repository point plus fresh PostgreSQL
state. The scoped key must be able to read `immich/restic/`; routine recovery
does not require write or delete access even though the NAS replication key
has those narrowly scoped capabilities.

Use a trusted, isolated recovery workstation or a reviewed disposable host.
Do not print credentials, place them in shell history, or copy them into this
repository. The following examples assume the operator has exposed an rclone
S3 remote named `b2`, exported the same scoped key as `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`, set `B2_ENDPOINT` and `B2_BUCKET` from password-manager
values, and written the restic password to a mode-`0600` temporary file. The
direct restic repository URL is:

```text
s3:<B2-S3-endpoint>/<NAS-backups-bucket>/immich/restic
```

Record the recovery host, restic and rclone versions, repository ID, selected
snapshot ID and time, and Immich/PostgreSQL versions. Do not record secret
values or original filenames as routine evidence.

## Inventory and direct remote verification

Inventory B2 before restoring. Listing the repository should show `config` and
the ordinary restic `data`, `index`, `keys`, and `snapshots` trees beneath the
single application prefix. An empty prefix, a second nested `restic` directory,
or selected internal objects rather than the complete tree is a replication
failure, not a reason to initialize anything.

```bash
rclone lsf "b2:${B2_BUCKET}/immich/restic" \
  --recursive --files-only
rclone size "b2:${B2_BUCKET}/immich/restic"
RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE}" \
  restic -r "s3:${B2_ENDPOINT}/${B2_BUCKET}/immich/restic" snapshots --compact
RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE}" \
  restic -r "s3:${B2_ENDPOINT}/${B2_BUCKET}/immich/restic" check
```

Here `B2_BUCKET` is the bucket name from external escrow, not a guessed default.
The inventory uses rclone while restic accesses the same prefix directly
through B2's S3 endpoint; neither requires first copying repository objects to
the NAS. Treat any nonzero status as failure. Never run `restic init`, `forget`,
`prune`, `repair`, or `rclone sync` while investigating a remote repository. If
direct structural verification fails, preserve the B2 inventory and logs,
inspect hidden B2 object versions, and recover to a separate prefix or local
directory; do not overwrite the only off-site copy.

Select a snapshot only after its host, tags, source path, and time match the
expected Immich job. Restore directly from B2 into an empty local staging
directory and retain the restic log:

```bash
RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE}" \
  restic -r "s3:${B2_ENDPOINT}/${B2_BUCKET}/immich/restic" \
  restore "${RESTIC_SNAPSHOT_ID}" --target "${RESTORE_STAGING_DIRECTORY}"
```

The repository objects remain restic-encrypted in B2 and use the S3 transport
only as opaque objects; restic authenticates and decrypts file content at the
restore destination. If the archived source path is uncertain, first run
`restic ls` with the same `-r` repository option and selected snapshot ID.
Then identify the restored authoritative-library root. Do not flatten or
rearrange it until the restore has completed successfully.

## Disposable ZFS destination

On the NAS, an operator may copy the decrypted restore to a uniquely named,
empty disposable dataset. Agents must not execute these commands on the NAS.
Resolve and review the literal dataset and mountpoint first; never reuse a
production dataset or broad recursive target. A representative creation and
labeling sequence is:

```bash
sudo zfs create \
  -o mountpoint=/var/lib/nas-restore/immich-b2-library \
  tank/immich-b2-restore
sudo chown 51130:51130 /var/lib/nas-restore/immich-b2-library
sudo semanage fcontext -a -r s0 -t container_file_t \
  '/var/lib/nas-restore/immich-b2-library(/.*)?'
sudo restorecon -F -R -x /var/lib/nas-restore/immich-b2-library
```

Copy the restored authoritative tree into that mountpoint without crossing
filesystems unintentionally, then repeat `restorecon`. Do not recursively
normalize ownership: preserve restic's restored descendant IDs and require the
dataset root itself to use the production service identity. Verify a bounded
sample is within the service or subordinate-ID ownership contract and reports
`object_r:container_file_t:s0`. Preserve xattrs during transfer, but treat the
destination host's persistent fcontext rule as authoritative. Use separate
small scratch directories for the fresh PostgreSQL, Valkey, `thumbs`, and
`encoded-video` state required by the isolated application rehearsal.

## Reviewed restore sequence

Use the same Immich application version and compatible PostgreSQL major version
recorded in the dump filename whenever possible. Restore into fresh database
storage and a writable copy or clone of the authoritative library. Never mount
a production dataset writable into a rehearsal.

1. Record the database dump filename, size, modification time, Immich version,
   PostgreSQL version, and configured retention. Run `gzip --test` on the dump.
2. Capture or select the filesystem point taken after the dump. For a local
   rehearsal, create a manually named ZFS snapshot and a writable clone with an
   unmistakably disposable dataset name. For B2, restore the selected restic
   snapshot into the empty disposable dataset described above.
3. Give the clone the production service identity and
   `container_file_t:s0` SELinux contract. Use small privately relabeled
   scratch directories for fresh PostgreSQL and Valkey state.
4. Initialize the pinned PostgreSQL image with a disposable password. Restore
   the dump with `psql --single-transaction --set ON_ERROR_STOP=on`, applying
   Immich's documented `search_path` replacement to the SQL stream.
5. Confirm that the restored database has users and assets, and that PostgreSQL
   reports the expected major version. Do not print user names, asset names, or
   paths as routine evidence.
6. Start disposable Valkey and the pinned Immich server on an isolated network.
   Publish the server on NAS loopback only and reach it through an SSH tunnel.
   Machine learning is not required for recovery validation.
7. When generated datasets are excluded, create empty `thumbs` and
   `encoded-video` anchors and `.immich` markers only in disposable storage.
   Label both anchors and markers `container_file_t:s0`. Broken timeline
   previews are expected until thumbnails are regenerated.
8. Compare every active `/data/upload/` and `/data/library/` database path with
   the disposable filesystem. Require zero missing source files and zero
   unexpected path prefixes. Report `/data/encoded-video/` records separately.
9. Log in through the isolated web UI. Verify representative older and recent
   originals, an original download, dates, and album membership. Do not run
   bulk thumbnail, transcoding, or machine-learning jobs merely to make the
   rehearsal UI attractive.
10. Stop and remove the disposable containers and network, destroy only the
    explicitly named clone or B2 restore dataset and rehearsal snapshot, remove
    the matching scratch directory and temporary recovery credentials, and
    remove the temporary fcontext rule if its path will not be reused. Recheck
    that production Immich remains healthy.

For a real recovery, replace the local ZFS snapshot with the authoritative
filesystem copy from the backup destination, install the real runtime secrets,
restore ownership and persistent SELinux policy, and expose Immich only after
the database-to-source-file validation passes.

## R2 rollout and completion gate

Provisioning status as of 2026-08-30:

- the private B2 bucket and `immich/restic/` prefix lifecycle rule are in place;
- the prefix-scoped read/write application key is in place;
- all five backup secrets contain their real values in SOPS; and
- the restic encryption password is escrowed in the administrator's external
  password manager.

Image deployment, explicit repository initialization, first backup and mirror,
and the isolated direct-B2 restore rehearsal remain pending. The broader R3
recovery-material verification remains separate from this provisioning status.

Before the first run, the operator manually creates one private NAS-backups B2
bucket, the `immich/restic/` application prefix, and an application key limited
to that bucket prefix with only the read, write, delete, and listing abilities
needed by replication and verification. Configure a prefix lifecycle rule with
`daysFromHidingToDeleting: 30` and no upload-age hiding. This retains replaced
or deleted versions temporarily without hiding live restic objects.

Through the existing SOPS workflow, the operator supplies
`immich-backup-restic-password`, `immich-backup-b2-key-id`,
`immich-backup-b2-application-key`, `immich-backup-b2-bucket`, and
`immich-backup-b2-s3-endpoint`. Copy the strong generated restic password,
bucket identity, scoped B2 credential, and minimum recovery instructions to the
external password manager before initialization. Do not commit plaintext or
send it back as command output.

After deployment through the normal main-branch image flow, the reviewed
operator sequence is:

```bash
sudo nas-backup-immich status
sudo nas-backup-immich init
sudo nas-backup-immich status
```

Initialization is explicit and only for a confirmed-empty local repository; an
access or password failure must never trigger reinitialization. Observe the
potentially multi-hour first upload through systemd status, secret-safe logs,
backup metrics, and B2 inventory. Then perform the direct-B2 isolated restore
above, including the complete database-to-authoritative-file comparison,
representative older and recent UI checks, an original download, date and
album verification, disposable-resource cleanup, and production Immich health
revalidation.

Do not mark roadmap R2 complete based on successful initialization, upload,
repository checks, or metrics alone. The isolated restore from B2 and all
acceptance and cleanup checks are the completion gate. Record the rehearsal
date, selected snapshot, versions, counts, and secret-safe outcome in this
document when it passes.

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
