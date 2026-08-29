# Immich recovery runbook

## Scope

This is the authoritative Immich restore runbook. It currently covers selecting
and validating a local recovery point, restoring it into isolation, proving the
application-level result, and tearing the rehearsal down safely. The off-site
replication work in roadmap item R2 will extend the front of this runbook with
remote inventory, retrieval, decryption, integrity verification, and
fresh-host bootstrap steps. Those additions do not replace the recovery-unit,
restore, or acceptance contracts proved here.

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

## Reviewed restore sequence

Use the same Immich application version and compatible PostgreSQL major version
recorded in the dump filename whenever possible. Restore into fresh database
storage and a writable copy or clone of the authoritative library. Never mount
a production dataset writable into a rehearsal.

1. Record the database dump filename, size, modification time, Immich version,
   PostgreSQL version, and configured retention. Run `gzip --test` on the dump.
2. Capture or select the filesystem point taken after the dump. For a local
   rehearsal, create a manually named ZFS snapshot and a writable clone with an
   unmistakably disposable dataset name.
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
10. Stop and remove the disposable containers and network, destroy the clone
    and rehearsal snapshot, and remove only the explicitly named scratch
    directory. Recheck that production Immich remains healthy.

For a real recovery, replace the local ZFS snapshot with the authoritative
filesystem copy from the backup destination, install the real runtime secrets,
restore ownership and persistent SELinux policy, and expose Immich only after
the database-to-source-file validation passes.

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
