# Roadmap

The platform migration is complete. The current goal is to keep the NAS
boring, make service additions declarative, and spend maintenance effort only
where production evidence justifies it.

## Current platform

- Eighteen image-defined services run as dedicated rootless users under libkrun,
  including the production-validated Sonarr, Radarr, Prowlarr, and SABnzbd
  media-automation services.
- Each microVM has a generated root-managed routed TAP, `/30`, DHCP lease,
  nftables policy, and fail-closed readiness lifecycle.
- `quadlets/*.toml` is the service source of truth. The typed Python compiler
  validates the fleet and generates Quadlets, identities, subordinate IDs,
  TAP configuration, nftables policy, and consumer-specific fleet manifests.
- SOPS is decrypted once by a root-owned boot service into service-owned files
  below `/run/nas-secrets/`. Rootless containers mount only their copies.
- ZFS-backed services use dedicated host preparation units and persistent
  SELinux labeling; large datasets are not recursively relabeled by Podman.
- CI runs strict typing, behavioral tests, generated-artifact parity, image
  builds, signature verification, and publication with pinned actions.

Historical design and rollout evidence remains below `docs/history/`. That
subtree explains why current invariants exist but is not part of the active
architecture or operating instructions.

## Settled invariants

1. Rootless secrets are runtime files, not Podman `Secret=` objects.
2. Generated files with a `GENERATED` header are never hand-edited.
3. Every service identity has an allocate-only UID and a non-overlapping
   subordinate ID range. Retired UIDs must never be reused.
4. TAP guests start only after the current-boot network-policy marker exists;
   stopping networkd or nftables first quiesces the service user managers.
5. Cross-manager startup requirements use typed, bounded readiness checks,
   not raw systemd directive escape hatches.
6. Image assets live under `/usr/share/nas`; mutable state lives
   under `/var`; large ZFS data keeps host-managed SELinux labels.
7. The production NAS is never an agent execution target. Operators run the
   smallest reviewed commands when live evidence is necessary.

## Operating posture

It is reasonable to let the system coast. There is no planned platform
migration and no need to add services merely to keep the project moving. While
coasting, respond to alerts, keep normal dependency updates flowing, and turn
production surprises into a test or runbook entry. Manual TPM enrollment, SOPS
age-credential installation, and occasional `tank` import remain accepted
single-admin bootstrap rather than backlog items.

The backlog below is ordered by risk reduction and leverage, not novelty.
Items within a tier can be selected independently unless a dependency is
called out. A task is not active merely because it is listed here.

## Tier 0: protect irreplaceable data

- [x] **R1 — Rehearse an isolated Immich restore.** Completed 2026-08-29 with
  an accepted 24-hour database RPO. A retained Immich dump restored
  transactionally into fresh PostgreSQL storage and ran with a writable clone
  of the later authoritative-library point. All 11,810 authoritative source
  records resolved to files; authentication, representative photos, an
  original download, dates, and albums were verified. Disposable resources
  were removed and production health rechecked. The reviewed procedure,
  versions, evidence, generated-data classification, and SELinux details are
  in [`operations/immich-restore.md`](operations/immich-restore.md).
- [ ] **R2 — Back up Immich to encrypted restic storage and Backblaze B2.** A
  daily host runner pairs the newest validated Immich SQL dump with a later,
  nonrecursive snapshot of `tank/immich-server/library`. A no-network libkrun
  guest writes an encrypted local restic repository; a separate guest, which
  never receives the restic password or photo-library mount, mirrors the
  repository byte-for-byte to `immich/restic/` in a private B2 bucket. Keep 14
  daily, 8 weekly, 12 monthly, and 3 yearly restore points; bound uploads to
  5 MiB/s; alert on stale local or remote success, failed runs, stale integrity
  verification, and low `/var` capacity; and perform weekly local and remote
  structural checks with a rotating remote data subset. Operator provisioning
  completed 2026-08-30: the private bucket, `immich/restic/` lifecycle rule,
  prefix-scoped application key, five real SOPS values, and external escrow of
  the restic encryption password are in place. Deployment and local repository
  initialization/check succeeded; the first mirror exposed the rejected passt
  path, and a generated root-owned `krun-backup` TAP correction now awaits
  deployment. The first B2 backup and validation remain pending. R2 is not
  complete merely when the first upload succeeds: it requires a full isolated restore from B2,
  representative UI and original-download checks, cleanup of disposable
  resources, and production-health revalidation. See
  [`proposals/application-backups.md`](proposals/application-backups.md) and
  [`operations/immich-restore.md`](operations/immich-restore.md).
- [ ] **R3 — Protect the recovery inputs themselves.** Document and test access
  to the SOPS age key, LUKS recovery material, the Immich B2 recovery
  credential, the restic repository password, the bucket name and endpoint,
  and the minimum fresh-host bootstrap sequence. Keep the B2 and restic
  recovery material in the administrator's external password manager rather
  than only on the NAS or in this repository. Secret values remain outside the
  repository. This can be completed alongside R2.

## Tier 1: finish existing safety and visibility work

- [ ] **R4 — Add a NAS overview dashboard.** Make one provisioned landing page
  answer: are user-facing endpoints reachable, are alerts firing, are all
  scrapes fresh, is host/ZFS capacity healthy, are snapshot timers succeeding,
  and is log ingestion current? Use blackbox results for Garage and other
  user-facing availability; do not substitute Garage's slow admin scrape.
  Prefer an application-level row over eighteen equally prominent component
  rows, with links to service dashboards and logs. Completion includes a
  missing/stale-data state, not only a green state.
- [ ] **R5 — Complete the staged journald rollout.** Production-validate the
  already-configured groups in order: observability/edge, Immich, Jellyfin,
  then media automation. Each group retains read-only, VictoriaLogs-outage,
  Vector-restart, and clean-reboot gates before the next is enabled. Archive
  closeout evidence and simplify the active status afterward. The exact
  sequence remains in [`operations/logging.md`](operations/logging.md).
- [ ] **R6 — Run the first real boot-image VM smoke.** The safe QCOW2/QEMU
  harness exists and its runner behavior is tested, but no end-to-end guest
  pass has been recorded. `bcvk` 0.18.0 is installed on the development host.
  First use `bcvk to-disk --format=qcow2` to create a fresh image for the
  existing `make test-vm` contract. Then spike `bcvk ephemeral run --ignition`
  as a possible simpler runner; keep the existing no-network, no-production-
  storage, no-secrets boundary. Upstream documents ephemeral, to-disk, and
  libvirt workflows in the [bcvk project](https://github.com/bootc-dev/bcvk).
  Do not replace the current harness until bcvk proves the same isolation and
  assertions.

## Tier 2: improve daily operation

- [ ] **R7 — Operationalize BuildKit provenance and SBOM attestations.** The
  publisher currently disables both while separately attaching a GitHub
  artifact attestation. Enable BuildKit `provenance: mode=max` and `sbom: true`
  only together with consumers that make them release evidence. After push,
  verify by digest that provenance names this repository, commit, workflow,
  builder, platforms, and expected build materials; reject an absent or
  mismatched predicate. Consume the SPDX SBOM in a vulnerability-policy step
  and retain a human-usable package inventory for incident response and image
  comparison. Define severity, fix-availability, exception, and stale-database
  behavior before making scanning blocking. Ensure no secret is passed through
  build arguments because max provenance records their values. Reconcile the
  existing `actions/attest` step explicitly: retain it only if its GitHub
  verification path adds evidence not supplied by the BuildKit provenance,
  otherwise remove the duplicate. The current simultaneous `push: true` and
  `load: true` workflow also needs a tested output design because the local
  Docker image store does not preserve attestations; continue exact-image
  contract verification against the same published digest rather than
  weakening that gate. Apply this reusable verification contract to R8, R12,
  and R13 as those owned artifacts are introduced. See Docker's official
  [attestation guidance](https://docs.docker.com/build/ci/github-actions/attestations/)
  and [SBOM verification guidance](https://docs.docker.com/build/metadata/attestations/sbom/).
- [ ] **R8 — Move the Caddy repack into this repository.** Retire the separate
  `lab-caddy` repository and build its contents here as
  `ghcr.io/samhclark/nas/caddy-repack`. Preserve the current Caddy and
  Cloudflare-module version pins, multi-architecture expectations, labels,
  SBOM/provenance, and digest-pinned Quadlet consumption. Add it to this
  repository's existing build/release workflow rather than creating another
  independent CI/CD path. Prove configuration validation, startup, metrics,
  TLS issuance/renewal, and rollback before retiring the old image location.
- [ ] **R9 — Fill dashboard coverage by application.** Existing provisioned
  dashboards cover ZFS/disk health, Garage, Jellyfin, vmalert, and
  Alertmanager. Add focused views for ingress/logging/metrics, Immich, and the
  media-automation stack before considering one dashboard per microservice.
  Caddy, Grafana, VictoriaMetrics, VictoriaLogs, and blackbox-exporter can share
  an observability-platform view; Immich's four components and the four media
  services should be application views. Add a separate service dashboard only
  when it has useful service-specific metrics or log queries. Every dashboard
  should expose health, resource pressure, errors, and a link to relevant
  logs, while avoiding secrets and high-cardinality personal labels.
- [ ] **R10 — Build a small `nas` operator CLI.** Start by inventorying repeated
  read-only operations and fold the existing `nas-diagnose-immich` behavior
  into commands such as `nas status`, `nas status <application>`, `nas logs
  <service>`, and `nas diagnose <application>`. Derive identities, units, and
  endpoints from generated fleet manifests rather than creating another
  service registry. Print the exact privileged command before any mutation;
  keep restart, repair authorization, restore, and VM execution as explicit
  subcommands rather than hiding them behind a generic shell. The CLI should
  improve operator ergonomics without becoming a second orchestrator.
- [ ] **R11 — Expand VM-backed integration coverage.** After R6, add one
  disposable scratch-disk ZFS acceptance test, populated-storage readiness
  reruns, service-user Quadlet generation/startup, and selected network-policy
  failure tests. Use bcvk's disposable disk attachment where useful. Keep
  destructive fixtures structurally unable to see production block devices,
  pools, credentials, or networks. Networked image/runtime compatibility
  probes remain opt-in.

## Tier 3: experiments and bounded technical debt

- [ ] **R12 — Evaluate owned media-automation images.** Sonarr, Radarr, and
  Prowlarr have the broadest exposure to untrusted indexer content and require
  repo-owned libkrun/logging entrypoint adapters. Decide explicitly whether a
  thin repack of the pinned LinuxServer images is enough or whether the threat
  justifies reproducible builds from upstream application sources on a
  smaller owned base. A thin derivative brings the adapter and image contract
  together but does not make inherited binaries more trustworthy. Start with
  one service, produce an SBOM and provenance, scan the final image, preserve
  non-root storage and signal behavior, and pass the existing ordinary-crun,
  libkrun, network, egress-kill-switch, populated-storage, and rollback gates.
  Extend the pattern only if the maintenance and update burden is justified.
  Evaluate SABnzbd separately under the same outbound-content threat even
  though it is not a Servarr application.
- [ ] **R13 — Publish patched crun as a repository-owned OCI artifact.** Move
  the current source verification, libocispec pins, patch application, build,
  and binary assertions into a dedicated image target, likely
  `ghcr.io/samhclark/nas/crun`. Publish it only when the crun version, patch
  set, toolchain/base, or target architecture changes; have the bootc build
  copy the verified binary from a digest-pinned artifact instead of compiling
  it every time. The artifact needs architecture and Fedora ABI compatibility
  metadata, an SBOM/provenance record, signature verification, a smoke proving
  both ordinary OCI and `krun.tap_name` behavior, and a documented rollback.
  Keep the patch source in this repository so the published binary remains
  reproducible and reviewable with the bootc image that consumes it.
- [ ] **R14 — Evaluate a libkrun block-device annotation.** Before patching
  crun, confirm the current libkrun API and lifecycle, then prototype with a
  disposable file-backed block device. If R13 proves the artifact pipeline,
  use it for this new patch rather than returning compilation to the bootc
  build. Use bcvk's `--mount-disk-file` support to help exercise guest
  filesystem and failure behavior. Only then evaluate a managed zvol for
  PostgreSQL-shaped workloads. The design must cover rootless device access,
  SELinux, stable device identity, guest formatting and fsck, resize,
  snapshot/backup consistency, discard, teardown, and prevention of
  cross-service attachment. Do not migrate the Immich database until the
  file-backed prototype, crash recovery, backup, restore, and rollback path
  are all proven; preserving a host-visible filesystem may remain the simpler
  operational choice.
- [ ] **R15 — Resolve known runtime test gaps.** Add a disposable systemd-user
  test for early container exit during the post-start listener probe, then
  avoid leaving the unit in `activating` for the full timeout. Separately test
  the real media-automation guest-root filesystem/credential path under
  libkrun; current adapter unit tests do not prove that boundary. See
  [`architecture/libkrun-networking.md`](architecture/libkrun-networking.md)
  and [`development/rootless-quadlets.md`](development/rootless-quadlets.md).
- [ ] **R16 — Continue Jellyfin hardware-transcoding research only when useful.**
  Preserve software processing and the VM boundary as the supported state.
  Follow the narrow next experiment in
  [`investigations/jellyfin-hardware-transcoding.md`](investigations/jellyfin-hardware-transcoding.md)
  rather than reopening ordinary-crun device passthrough as production.

## Parking lot and revisit triggers

- Caddy access-log ingestion and Mullvad/WireGuard host logging are separate
  projects after R5, not blockers for the service journald rollout.
- Candidate-to-stable image promotion becomes worthwhile when R6/R11 provide an
  automated candidate gate, when there is more than one consumer, or when host
  update retry behavior changes.
- Add a retired-UID registry check when the first image-managed service is
  actually retired; allocated numeric identities are never reused.
- New services require an explicit storage, exposure, secrets, monitoring, and
  backup-or-no-backup disposition. There is currently no service-addition
  backlog.

## Adding a service

1. Allocate a namespaced `_nas_*` identity and never-reused UID in the bucket
   documented by `AGENTS.md`.
2. Add one strict `quadlets/<service>.toml` with its container, TAP, storage,
   asset, secret, and typed startup contracts.
3. Add image-controlled assets or a specialized ZFS preparation unit only
   when the service actually needs them. Add encrypted values to the SOPS file
   for every declared secret.
4. Run `make generate-quadlets`. The compiler derives account enablement,
   subordinate IDs, service files, network policy, secret routing, and asset
   labeling inventories.
5. Run `make check`, `make test`, and `make build`, then commit the source and
   generated artifacts together.
6. Add a Caddy route and service-specific operator checks only if the service
   is user-facing.

If adding a service requires duplicating an identity list, manually editing a
generated unit, or parsing TOML in shell, fix the platform boundary instead.
