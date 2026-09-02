# Documentation map

The live system is described by a small set of authoritative documents. Start
here rather than reading rollout plans or evidence logs.

## Architecture

- [`architecture/applications.md`](architecture/applications.md) — application
  grouping, service roles, named endpoints, and startup dependencies
- [`architecture/secrets.md`](architecture/secrets.md) — encrypted source,
  boot-time distribution, and the rootless runtime-file boundary
- [`architecture/libkrun-networking.md`](architecture/libkrun-networking.md) —
  routed TAP data plane and fail-closed lifecycle
- [`architecture/release-and-testing.md`](architecture/release-and-testing.md) —
  development gates, publishing policy, and deliberately deferred tests
- [`architecture/storage.md`](architecture/storage.md) — typed service storage,
  generated host preparation, and non-destructive repair policy

## Development and operations

- [`development/rootless-quadlets.md`](development/rootless-quadlets.md) — add or
  change a generated rootless service
- [`roadmap.md`](roadmap.md) — prioritized backlog, revisit triggers, and
  settled invariants
- [`operations/jellyfin-monitoring.md`](operations/jellyfin-monitoring.md) —
  playback monitoring setup and interpretation
- [`operations/immich.md`](operations/immich.md) — production topology,
  recovery classification, first use, and operator verification
- [`operations/immich-restore.md`](operations/immich-restore.md) — accepted
  recovery objective, direct B2 recovery, reviewed restore sequence, and
  rehearsal evidence
- [`operations/immich-backup-vm-smoke.md`](operations/immich-backup-vm-smoke.md)
  — production-shaped local ZFS/restic/libkrun recovery-point gate
- [`operations/logging.md`](operations/logging.md) — host journald, Vector,
  VictoriaLogs pilot, and staged operator validation
- [`operations/media-automation.md`](operations/media-automation.md) — the
  deployed Sonarr, Radarr, Prowlarr, and SABnzbd topology, configuration, and
  operator runbook
- [`investigations/jellyfin-hardware-transcoding.md`](investigations/jellyfin-hardware-transcoding.md)
  — the one active investigation

## Proposals

- [`proposals/application-backups.md`](proposals/application-backups.md) —
  accepted Immich-specific restic/B2 boundary, isolation model, retention,
  provisioning, monitoring, and completion gate

Completed migration plans and rollout evidence are retained only for
provenance. They are not current instructions and live under
`docs/history/`.
