# Application backup and replication proposal

Status: discussion only. This document records a direction; it is not an
implemented runtime contract or an instruction to enable backup jobs.

## Problem

Backups should describe a recoverable application, not merely a list of
directories or datasets. Multi-component applications such as Immich combine
state with different consistency and recovery requirements: original assets,
database records, configuration, generated derivatives, and caches. The *arr
stack adds shared media and download trees whose ownership does not align with
one component or even one application's backup boundary.

Local ZFS snapshots are useful rollback points, but they are not sufficient
backups because they share the NAS's failure domain. Off-site replication may
use different destinations and retention policies. Small, irreplaceable data
could reasonably be replicated to rsync.net or Backblaze B2, while large media
libraries may be deliberately excluded because their storage and restore cost
would exceed their value.

## Proposed principles

1. Start from the recovery outcome: which applications and shared data must be
   restorable, to what point in time, and with what acceptable data loss.
2. Keep application membership, storage ownership, and backup selection as
   separate concepts. Membership can help coordinate quiescing without making
   every mounted dataset part of the same backup.
3. Distinguish irreplaceable inputs and databases from reproducible derivatives
   and caches. Do not upload thumbnails, transcodes, model caches, or temporary
   downloads merely because they are mounted by the application.
4. Treat database and filesystem consistency as an application-level concern.
   For Immich, a usable recovery point must pair its database with the asset
   files that database references.
5. Make exclusions explicit. In particular, media should not silently enter an
   off-site policy through application membership or a shared mount.
6. Separate local snapshots, off-site replication, retention, verification,
   and restore rehearsal. Success in one stage must not imply the others work.
7. Prefer generated inventories and validation where they prevent omissions,
   but keep unusual quiesce, dump, and restore procedures explicit and
   reviewable until repeated production needs justify a typed abstraction.

## Possible future shape

A future application-level backup declaration might select named storage
resources, declare a consistency procedure, and assign each selected resource
to one or more destinations. The compiler could derive dataset inventories,
reject accidental inclusion of explicitly excluded media, and coordinate
bounded stop/start ordering for the application's components.

That future language should be driven by the concrete Immich recovery design
and at least one additional application such as the *arr stack. This proposal
does not add backup fields to today's service TOML and does not authorize a
generic hook or arbitrary-shell mechanism.

The initial Immich deployment provides useful classification boundaries without
implementing backup automation. `tank/immich-server/library` contains the
authoritative photo library, profiles, and Immich's database-backup output;
`tank/immich-database/data` contains the live database. The separate `thumbs`
and `encoded-video` datasets plus the machine-learning caches are generated
data and should not enter an off-site policy by default. A recovery design must
still pair a database recovery point with the library it describes.

## Decisions and remaining questions

The 2026-08-29 isolated Immich rehearsal resolved the application-specific
recovery questions. Immich accepts a 24-hour database RPO. Its recoverable unit
is a database dump plus a later authoritative-library point; thumbnails,
encoded video, Valkey, and machine-learning state remain excluded. Verification
uses a fresh transactional database restore, complete database-to-source-file
existence checks, and isolated UI review. The concrete procedure and evidence
are in [`../operations/immich-restore.md`](../operations/immich-restore.md).

The destination and automation questions below remain for off-site replication:

- Which destination and retention policy should protect the selected
  authoritative unit?
- How are encryption, credentials, bandwidth limits, retention, and deletion
  protection handled per destination?
- How does automation monitor dump freshness, replication, and periodic
  integrity checks without treating a local snapshot as an independent backup?
