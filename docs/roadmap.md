# Roadmap

The platform migration is complete. The current goal is to keep the NAS
boring, make service additions declarative, and spend maintenance effort only
where production evidence justifies it.

## Current platform

- Fourteen image-defined services run as dedicated rootless users under libkrun.
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

## Active work

1. **Jellyfin hardware transcoding.** Preserve the libkrun VM boundary while
   investigating safe Intel media-device exposure. Software processing is the
   production configuration until that evidence is complete. See
   `docs/investigations/jellyfin-hardware-transcoding.md`.
2. **Service expansion.** Immich first use, uploads, cross-device viewing, and
   a clean post-fix reboot are validated; use the selected media automation
   services as the next application-group proving case. Add candidates only
   when storage, exposure, secrets, monitoring, and the explicit
   backup-or-no-backup disposition are understood.
3. **Jellyfin operational validation.** Continue representative playback,
   monitoring, and recovery checks without weakening the VM boundary or
   privacy-limited exporter contract.
4. **Host logging rollout.** The Caddy and VictoriaMetrics journald sources
   established the transport pilot. The first expansion group—Garage,
   vmalert, Alertmanager, blackbox-exporter, Grafana, and Jellyfin exporter—is
   configured but remains awaiting production validation. Validate it through
   read-only, backend outage, Vector restart, and clean reboot stages. The
   Immich server, PostgreSQL, Valkey, and machine-learning group is the next
   configured expansion and must wait for that evidence before deployment.
   Jellyfin runtime logging is also configured behind both earlier gates while
   preserving its local application and transcode diagnostics. The
   Sonarr, Radarr, Prowlarr, and SABnzbd logging group is configured behind all
   three earlier gates. Their repo-owned adapters select structured Servarr
   output and explicit SABnzbd console output without disabling application
   file logs, so vendoring is not needed for this migration. Generate Vector's
   UID sources from the typed Quadlet declarations so collection membership
   cannot drift from service identity.
   Caddy access logs and host
   Mullvad/WireGuard logs remain separate projects. See
   `docs/operations/logging.md`.
5. **Documentation maintenance.** Keep README focused on operators and
   `AGENTS.md` focused on execution boundaries and invariants; remove stale
   rollout instructions as the platform evolves.
6. **Manual bootstrap.** TPM enrollment for non-root volumes, the SOPS age
   credential, and occasional `tank` import remain intentionally manual for
   this single-admin reference deployment.

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
