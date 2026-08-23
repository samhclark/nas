# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## Overview

This repository creates a NAS container image with ZFS, Tailscale, and encrypted storage support. The project has been **successfully overhauled** from a build-from-source approach to using prebuilt ZFS kernel modules with full CI/CD automation.

**Status**: In production for one personal NAS
**Build Time**: ~2-3 minutes (down from 10+ minutes)
**Container Registry**: `ghcr.io/samhclark/nas/bootc:stable`
**Ignition File**: `https://samhclark.github.io/nas/ignition.json`

This is an open source reference project, not a general-purpose appliance. The intended deployment is one machine for one user.

## Project Relationship

This project depends on `../fedora-zfs-kmods/` which builds and publishes prebuilt ZFS kernel modules as container images. The architecture uses registry-based compatibility checking - if a ZFS+kernel combination exists in the fedora-zfs-kmods registry, it's compatible.

## Fast Orientation

The quickest accurate mental model is:

1. `Makefile` and `.github/workflows/` decide **what versions to build**
2. `Containerfile` decides **what goes into the bootc image**
3. `overlay-root/` decides **how the installed machine behaves at runtime**
4. `butane.yaml` is intentionally narrow and personal: it handles host identity and root storage setup, not service orchestration

If you need to understand real behavior, prioritize `Containerfile`, `overlay-root/`, and `butane.yaml` over prose docs.

## Runtime Topology

This repo is not just a bootc image with ZFS. It currently defines a full single-node NAS host profile.

### Active Quadlet Containers

These are considered active and in use on the real machine unless explicitly stated otherwise:
- `blackbox-exporter.container` - local HTTP/TCP probe exporter for service-availability checks; rootless under `etc/containers/systemd/users/51230/`
- `caddy.container` - reverse proxy / TLS termination for the user-facing services; rootless under `etc/containers/systemd/users/51310/`, connected to the root-managed routed libkrun TAP network
- `garage.container` - S3-compatible object storage on ZFS; rootless under `etc/containers/systemd/users/51110/`, deployed and validated on the NAS
- `immich-server.container` - Immich API/web and photo-library service; rootless under `etc/containers/systemd/users/51130/`, first-use uploads and cross-device viewing validated on the NAS
- `immich-database.container` - dedicated Immich PostgreSQL/VectorChord database; rootless under `etc/containers/systemd/users/51140/`, deployed and validated on the NAS
- `immich-valkey.container` - dedicated Immich queue/cache service; rootless under `etc/containers/systemd/users/51150/`, deployed and validated on the NAS
- `immich-machine-learning.container` - CPU-only Immich machine-learning service; rootless under `etc/containers/systemd/users/51160/`, deployed and validated on the NAS
- `jellyfin.container` - media library and streaming server; rootless under `etc/containers/systemd/users/51120/`, deployed under libkrun with software media processing while VM-isolated hardware transcoding remains under investigation
- `jellyfin-exporter.container` - privacy-bounded Sessions API exporter for playback/transcode dashboards; rootless under `etc/containers/systemd/users/51260/`
- `victoria-metrics.container` - metrics storage; rootless under `etc/containers/systemd/users/51250/`, deployed and validated on the NAS
- `victoria-logs.container` - seven-day searchable log storage for the native
  Vector collector; rootless under `etc/containers/systemd/users/51270/`,
  deployed and validated through the initial Caddy and VictoriaMetrics pilot
- `vmalert.container` - alert rule evaluation; rootless under `etc/containers/systemd/users/51220/`
- `alertmanager.container` - notification fanout; rootless under `etc/containers/systemd/users/51240/`, deployed and validated on the NAS
- `grafana.container` - dashboards; rootless under `etc/containers/systemd/users/51210/`

### Configured, Awaiting Production Validation

These Quadlets are image-defined but must not be described as deployed until
their service-specific NAS validation is complete:
- `sonarr.container` - TV-library automation; rootless under `etc/containers/systemd/users/51410/`
- `radarr.container` - movie-library automation; rootless under `etc/containers/systemd/users/51420/`
- `prowlarr.container` - indexer management; rootless under `etc/containers/systemd/users/51430/`
- `sabnzbd.container` - Usenet downloader; rootless under `etc/containers/systemd/users/51440/`

### Supporting Host Units

Important non-container units:
- `nas-vector.service` - hardened native collector for selected rootless-service
  journald records; the Caddy and VictoriaMetrics pilot is deployed and
  validated, while later collection groups retain their own rollout gates
- `sops-distribute-secrets.service` - decrypts the repo-managed SOPS file and writes per-service runtime secret files at boot
- `nas-prepare-<service>-storage.service` - generated preparation and current-boot readiness for every stateful service, including all four Immich components
- `disk-health-metrics.timer` - emits SMART and ZFS metrics for node_exporter
- `zfs-snapshots-*@.timer` - rolling snapshot retention for selected datasets

### Monitoring Notes

- Garage service availability should be based on the blackbox-exporter probe of `http://garage.krun:3903/health`, not on `up{job="garage"}` from the admin `/metrics` scrape
- Garage's `/metrics` endpoint is still useful for internal/storage metrics, but it can respond slowly enough to create false `up == 0` results even when the service is healthy

### Storage Layout Assumptions

- Root filesystem is LUKS + btrfs, unlocked by TPM, without PCR binding
- The main data pool is expected to be `tank`
- Garage datasets live under `tank/garage/{meta,data}`
- VictoriaMetrics data lives under `tank/victoria-metrics/data`
- VictoriaLogs data lives under `tank/victoria-logs/data`; it has no snapshot
  or backup policy
- Jellyfin state lives under `tank/jellyfin/{config,cache}`; `tank/videos` is mounted at `/var/zfs/tank/videos`, with `movies` and `tv-shows` exposed read-only to Jellyfin
- The media-automation target layout is directory-based beneath the existing `tank/videos` dataset: `data/usenet/{incomplete,complete/{movies,tv}}` and `data/media/{movies,tv}`. Sonarr and Radarr mount the common `/data` root so imports can use hardlinks; SABnzbd mounts `/data/usenet`; Jellyfin mounts `/data/media` read-only.
- Required media layout paths are root-owned, group-owned by the fleet `media` GID `52000`, and setgid mode `2775`. Existing descendants retain their user owner during migration but receive the shared group and group access. The generated shared-storage unit validates the required-root contract but never creates, moves, chowns, or relabels production media automatically.
- `tank/videos` snapshots intentionally stop at five weekly snapshots (about one month); the downloaded media itself is intentionally outside the backup policy.
- Immich authoritative state lives under `tank/immich-server/library` and `tank/immich-database/data`; generated thumbnails and encoded video have separate datasets so a future backup policy can exclude them
- Large ZFS-backed container data paths are labeled persistently with `semanage fcontext` + `restorecon -F -R`; do not casually switch them to Podman `:Z` / `:z`
- `quadlets/*.toml` `[[storage]]` declarations are the source of truth for host creation/verification, SELinux policy, container mounts, and readiness; raw `/var` container volumes are rejected

### Secrets Model

- Secret material is encrypted in the repo with SOPS at `/usr/share/nas/secrets/secrets.sops.yaml`
- The SOPS age private key is expected on the NAS as a `systemd-creds` file at `/var/lib/nas-secrets/age-key.cred`
- `sops-distribute-secrets.service` is the boot-time source of truth for Garage, Caddy, VictoriaMetrics, Alertmanager, Jellyfin exporter, and Immich database secrets
- The root-owned distributor writes per-service runtime files under `/run/nas-secrets/<service>/`; consuming rootless services mount those files read-only instead of using Podman `Secret=`
- The Mullvad private key is the SOPS value `mullvad-private-key`. The distributor writes it as root-only runtime material for `systemd-networkd`, which receives it through a service credential; it is never rendered into a `.netdev` file.
- Rootless Podman secrets are not a validated production path. NAS testing showed that the former shell secret driver could not use meaningful `systemd-creds` key modes from rootless Podman's user-namespace context; see `docs/architecture/secrets.md` before changing the runtime-file design.

### Manual Bootstrap Reality

This repository intentionally still has some manual host bootstrap:
- non-root LUKS volumes are enrolled with TPM manually after install
- the SOPS age private key credential must be installed manually on the NAS
- `tank` may still need to be imported manually depending on system state

This is acceptable because the system has one real user and is published as a reference project, not as a turnkey product.

## NAS Execution Boundary

Agents must not SSH to or execute commands on the production NAS. When NAS
evidence or an operational action is needed, prepare a reviewed copy-paste
command for the operator, explain its effects, and wait for the operator to
return the output.

Prefer the smallest command that answers the current question. Keep
state-changing commands separate and explain them before asking the operator
to run them.

Changes committed and pushed to `main` are picked up by the scheduled image
build and NAS update. In a later session, assume such changes are deployed
unless the operator or current evidence says otherwise; do not add a manual
publish or deployment step by default.

## Key Commands

### Version Discovery & Compatibility
- `make versions` - Show ZFS, kernel versions and compatibility status
- `make zfs-version` - Get latest ZFS release
- `make kernel-version` - Get current CoreOS kernel version (script-based fallback if labels are missing)
- `make check` - Run static, non-mutating repository validation
- `make test` - Run behavioral unit and integration tests
- `make check-zfs-available` - Verify prebuilt ZFS kmods exist for current versions

### Building
- `make build` - Build image locally with automatic version discovery
- `make deps` - Verify required tools are present (Docker with Buildx, Podman, gh, skopeo, jq, uv)

### Ignition File Management
- `make generate-ignition` - Generate Ignition JSON from butane.yaml

### CI/CD Integration
- `make run-workflow` - Trigger main build workflow
- `make run-pages` - Trigger Ignition file generation and GitHub Pages deployment
- `make run-cleanup` - Trigger container cleanup (dry run)
- `make run-cleanup-force` - Trigger container cleanup (actual deletion)
- `make workflow-status` - Check build workflow status
- `make all-workflows` - Check status of all workflows

### Local Testing
- `make cleanup-dry-run RETENTION_DAYS=N` - Test cleanup logic with configurable retention

### Verification
- Run `make check` for static contracts and generated parity.
- Run `make test` for behavioral coverage. These are intentionally separate
  canonical commands.
- After changing `butane.yaml`: run `make generate-ignition` to verify the config is valid Butane.
- After changing `Containerfile` or `overlay-root/`: run `make build` to verify the image builds.
- These are independent — the Ignition file and the container image are separate artifacts with separate CI workflows.
- `bootc container lint` warnings about `/var` cache artifacts are currently expected and can be ignored for now. Warnings about `/var/usrlocal` usually mean something was copied into `/usr/local` before this image's overlay replaced Fedora CoreOS's default `/usr/local -> ../var/usrlocal` symlink.

## Architecture (Production)

**4-stage build process** consuming prebuilt ZFS RPMs:

### Stage 1: Build Patched crun

Build the pinned crun release with the repo's narrow `krun.tap_name` patch.

### Stage 2: Import SOPS

Copy the pinned SOPS binary from the upstream image whose signature CI
verifies.

### Stage 3: Pull Prebuilt ZFS Kernel Modules
```dockerfile
FROM ghcr.io/samhclark/fedora-zfs-kmods:zfs-${ZFS_VERSION}_kernel-${KERNEL_VERSION} as zfs-rpms
```

### Stage 4: Final Image Assembly

Starts `FROM quay.io/fedora/fedora-coreos:stable`, validates that the
provided `KERNEL_VERSION` matches the base image's actual kernel, then installs
the host packages (nftables, systemd-networkd, node-exporter, smartmontools,
tailscale, jq) plus the ZFS RPMs from stage 3, runs
`depmod`, and enables the systemd units. See the `Containerfile` itself for
the authoritative package and unit lists — do not duplicate them here.

## CI/CD Workflows

### Main Build (`.github/workflows/build.yaml`)
- **Trigger**: Daily at 9:18 AM UTC + manual
- **Jobs**: repository validation, SOPS verification, and version resolution → build
- **Output**: `ghcr.io/samhclark/nas/bootc:stable`
- **Features**: Version discovery, compatibility checking, build attestations

### Ignition Files (`.github/workflows/pages.yaml`)
- **Trigger**: Push to main (Butane config, Makefile generation logic, or Pages template changes) + manual
- **Output**: `https://samhclark.github.io/nas/ignition.json`
- **Features**: Butane→Ignition conversion, GitHub Pages deployment

### Container Cleanup (`.github/workflows/cleanup-images.yaml`)
- **Trigger**: Weekly Sundays 2 AM UTC + manual
- **Retention**: 90 days
- **Targets**: Both the current `nas/bootc` package and legacy `custom-coreos` versions
- **Safety**: Manual triggers default to dry-run

## Configuration Strategy

**This is a bootc-centric NAS system requiring careful separation of configuration approaches.**

### Containerfile Configuration (System Capabilities)
Use the `Containerfile` for configuration that adds **capabilities** to the system:
- **Security**: Sigstore verification for container pulls via `/etc/containers/policy.json` (used by bootc)
- **System Services**: NTP configuration, chronyd settings
- **Package Installation**: ZFS modules, nftables, systemd-networkd, Tailscale
- **Service Enablement**: systemd units (timers, tailscaled)

### Butane Configuration (Personal & Runtime)
Use `butane.yaml` for configuration that is **personal** or **cannot be described declaratively**:
- **Personal Settings**: SSH authorized keys, user password hash, hostname
- **Runtime Configuration**: LUKS encryption with TPM2 unlock
- **Dynamic Filesystem**: Encrypted btrfs mounting, partition layouts
- **Boot-time Decisions**: Anything requiring runtime system state

### Current Configuration (`butane.yaml`)
- **Encryption**: LUKS root filesystem with TPM2 unlock, without PCR binding
- **Filesystem**: Btrfs on `/dev/mapper/root`
- **Access**: SSH key and password hash for 'core' user
- **Identity**: Hostname set to 'nas'

### Installation URL
```
https://samhclark.github.io/nas/ignition.json
```

Use this URL during Fedora CoreOS installation to configure encrypted storage, SSH access, and system settings.

## Version Compatibility Strategy

**Registry-Based Compatibility**: No manual compatibility matrix maintenance.

- ✅ **If exists**: `ghcr.io/samhclark/fedora-zfs-kmods:zfs-X.X.X_kernel-Y.Y.Y` → Compatible
- ❌ **If missing**: Build fails early with clear error pointing to fedora-zfs-kmods project

This eliminates duplicate compatibility tracking and provides automatic compatibility validation.

## Container Labels

Images include labels for future deduplication:
- `nas.bootc.zfs-version` - ZFS version used
- `nas.bootc.kernel-version` - Kernel version used

## Key Files

### Core Files
- `Containerfile` - 4-stage build definition
- `butane.yaml` - Fedora CoreOS configuration with host identity + storage
- `Makefile` - Development commands (`make help` to see all targets)
- `ignition.json` - Locally generated, gitignored Ignition output
- `overlay-root/` - Systemd units, ZFS scripts, Quadlets, cosign policy files
- `scripts/query-coreos-kernel.sh` - Kernel version discovery (called by Makefile and CI)
- `scripts/resolve-zfs-version.sh` - ZFS version discovery (called by Makefile and CI)
- `scripts/select-expired-images.sh` - Local/CI GHCR cleanup query boundary
- `scripts/plan-image-cleanup.py` - Typed, pure package-version cleanup planner

### CI/CD Workflows
- `.github/workflows/build.yaml` - Main container build
- `.github/workflows/pages.yaml` - Ignition file serving
- `.github/workflows/cleanup-images.yaml` - Registry maintenance

### Documentation
- `docs/README.md` - Small index of authoritative architecture, development, and operations documentation
- `docs/architecture/secrets.md` - Current rootless runtime-secret boundary and supporting NAS evidence
- `docs/architecture/libkrun-networking.md` - Current routed-TAP data plane and fail-closed lifecycle
- `docs/architecture/release-and-testing.md` - Canonical gates, accepted publishing risk, and test boundaries
- `AGENTS.md` - This file
- `README.md` - User documentation
- `docs/development/rootless-quadlets.md` - Repo-specific pattern for migrating and creating rootless Quadlets
- `docs/operations/jellyfin-monitoring.md` - API-key bootstrap, privacy contract, and interpretation guidance for the Jellyfin playback dashboard
- `docs/investigations/jellyfin-hardware-transcoding.md` - Current evidence and decision log for preserving a VM boundary while pursuing Intel hardware transcoding
- `vendored-docs/podman-systemd.unit.5.md` - Vendored Quadlet reference, useful for rootless/systemd placement questions

## Development Patterns

**Registry-First Compatibility**: Let the container registry be the source of truth for ZFS+kernel compatibility rather than maintaining duplicate matrices.

**Local-First CI/CD Development**: Implement workflow logic in Makefile targets and `scripts/` first, then reference those scripts from GitHub Actions for consistency.

## bootc Primer (Tips)

- Treat `/usr` as immutable at runtime; bootc bind-mounts it read-only.
- Only `/var` persists across upgrades; avoid relying on updates to existing `/var` files from new images.
- Standard writable paths are symlinks into `/var` (e.g. `/home` -> `/var/home`, `/opt` -> `/var/opt`).
- Fedora CoreOS normally has `/usr/local -> ../var/usrlocal`, but this image intentionally ships image-managed files under `overlay-root/usr/local/`, so the deployed NAS has `/usr/local` as a real immutable directory.
- Use systemd `tmpfiles.d` or unit `StateDirectory=` to seed `/var` content on first boot.
- Prefer packaging static content into `/usr`; avoid dropping mutable content into `/var` during image builds.

## Host Service Identity Scheme

- Rootless service accounts should use namespaced host usernames such as `_nas_grafana` rather than upstream/vendor defaults like `grafana`
- Reserve `51000-51999` for image-managed service accounts in this repo
- Use category buckets inside that range: `511xx` for storage, `512xx` for observability, `513xx` for ingress/edge
- Current storage/application allocation: `_nas_garage` uses host UID/GID `51110`; `_nas_jellyfin` uses `51120`; `_nas_immichserver` uses `51130`; `_nas_immichdatabase` uses `51140`; `_nas_immichvalkey` uses `51150`; `_nas_immichmachinelearning` uses `51160`
- Current observability/edge allocation: `_nas_grafana` uses `51210`; `_nas_vmalert` uses `51220`; `_nas_blackbox` uses `51230`; `_nas_alertmanager` uses `51240`; `_nas_victoriametrics` uses `51250`; `_nas_jellyfinmetrics` uses `51260`; `_nas_victorialogs` uses `51270`; `_nas_caddy` uses `51310`
- Current media-automation allocation: `_nas_sonarr` uses `51410`; `_nas_radarr` uses `51420`; `_nas_prowlarr` uses `51430`; `_nas_sabnzbd` uses `51440`. These identities are configured but not production-validated yet.
- Subordinate ID ranges are a separate allocator, but keep them globally non-overlapping; the current convention is to derive a `65536`-wide range from the host UID for readability, e.g. `_nas_grafana:512100000:65536`
- UIDs are allocate-only: never reuse a UID from a retired service. File ownership is numeric and outlives the user — ZFS snapshots in particular can hand a retired UID's files to whatever service reuses it. `quadlets/*.toml` is the registry of active allocations; when the first service is actually retired, record its UID here as retired and add a `retired-uids` check to `generate-quadlets.py`.

## Rootless Quadlet Note

Current state:
- Fourteen image-defined services are deployed as rootless admin-managed user Quadlets: the existing ingress, observability (including VictoriaLogs), Garage, and Jellyfin services plus the four-component Immich application. Four media-automation services are configured but await their production validation. Immich first use and a clean post-fix reboot are validated; the earlier reboot exposed and motivated the mapped-ownership storage-readiness fix. Jellyfin's service path is operational, while representative playback and VM-isolated hardware transcoding remain active validation work.
- All eighteen configured services run under libkrun with explicit CPU and RAM annotations, `StopSignal=SIGINT`, and one root-managed routed TAP per microVM
- Rootless-service files are **generated**: edit `quadlets/<service>.toml`, run `make generate-quadlets`, and commit both. Each service declares an application, a unique role within that application, named endpoints with allowed consumers, and any bounded startup dependencies. Never hand-edit files with a `GENERATED` header — CI (`build-preflight.yaml` job `verify-repository`) fails on drift. The generated account-unit, storage-unit, secret, asset, and active-TAP manifests drive non-Python consumers; adding a service does not require a manual Containerfile enablement line. Add encrypted values to `overlay-root/usr/share/nas/secrets/secrets.sops.yaml` for declared secrets.

Useful reference points for future rootless work:
- The vendored `podman-systemd.unit.5.md` in this repo documents the rootless admin-managed Quadlet search paths under `/etc/containers/systemd/users/$(UID)` and `/etc/containers/systemd/users/`
- In practice, placing a user Quadlet under `/usr/share/containers/systemd/users/${UID}/` caused Fedora 43 with Podman 5.8.1 to generate a system unit in `system.slice`, because that path is still underneath the rootful `/usr/share/containers/systemd/` tree. Use `/etc/containers/systemd/users/${UID}/` for rootless service users in this repo.
- `sysusers.d` configuration belongs in `/usr/lib/sysusers.d` for packaged/vendor config; it is not a `/var` payload
- Rootless Podman expects subordinate ID ranges. This repo ships explicit ranges for every `_nas_*` service user in `/etc/subuid` and `/etc/subgid`
- If more rootless service users are added later, keep subordinate ID ranges non-overlapping and treat `/etc/subuid` and `/etc/subgid` as globally coordinated host resources
- Do not use Podman `Secret=` for rootless services. Use per-service runtime files written by the root-owned SOPS distributor under `/run/nas-secrets/<service>/`, mounted read-only with `:ro,Z` (validated on the NAS 2026-07-03: rootless Podman can relabel `/run` tmpfs files to `container_file_t`; unrelabeled `var_run_t` files are blocked by SELinux).
- linger state lives under `/var/lib/systemd/linger`; generated tmpfiles provisioning creates each service marker before logind starts
- Rootless user services should not depend directly on system units like `victoria-metrics.service`; cross-manager ordering is fragile, so prefer services that can tolerate starting independently, or use a bounded `ExecStartPre=` readiness loop when startup requires a local dependency to answer first
- Grafana's shipped provisioning and dashboards now live under `/usr/share/nas/grafana/` so they remain image-controlled rather than service-owned
- vmalert's shipped rules now live under `/usr/share/nas/vmalert/` so they remain image-controlled rather than service-owned
- Alertmanager's static config lives under `/usr/share/nas/alertmanager/` and uses native Pushover `user_key_file` / `token_file` settings; do not reintroduce plaintext config generation under `/var`
- Stateful service storage is declared under `[[storage]]`. Generated preparation units create only missing managed resources, verify exact mounts/properties, maintain persistent SELinux policy, and publish `/run/nas-storage/<service>/ready`; see `docs/architecture/storage.md`.
- Owned storage roots require the exact service UID/GID. Descendants may use that identity, a declared supplemental fleet GID, or IDs inside the service's generated subordinate range; container-root-created mount anchors and app files using the typed shared group are not ownership drift. The versioned storage manifest must carry this identity contract so it survives reboot checks. For descendant labels, enforce `object_r:container_file_t:s0` with no MCS categories rather than requiring a particular SELinux user field.
- Caddy, Alertmanager, Grafana, Immich Valkey, and Immich machine learning use small directory storage with guarded automatic repair. Garage, VictoriaMetrics, VictoriaLogs, Jellyfin, the Immich server, and the Immich database use explicit-repair ZFS storage; create `/var/lib/nas-repairs/<service>/repair-required` and restart `nas-prepare-<service>-storage.service` only after reviewing why the bounded check failed.
- Jellyfin's `tank/videos` declaration is required-existing, preserve-owner, and read-only. The generic runtime may repair labels after an explicit request but never creates that dataset or changes its ownership/modes.
- The image carries a narrowly scoped crun 1.29.1 patch adding `krun.tap_name`; it calls libkrun's upstream `krun_add_net_tap()` with the embedded DHCP client enabled. TAP-backed Quadlets must include `AddDevice=/dev/net/tun` because libkrun opens that device after entering the container mount namespace.
- `systemd-networkd` owns the generated `krun-*` TAPs and the host `wg-arr` WireGuard interface; NetworkManager explicitly ignores both patterns. Each service has a dedicated /30, a one-address DHCP pool with Rapid Commit, and a TAP owned by that service's host account. Generated nftables rules provide anti-spoofing, declared inter-service edges, host publication, and outbound NAT. Do not hand-edit generated `.netdev`, `.network`, or `nas-krun-*.nft` files.
- Sonarr, Radarr, Prowlarr, and SABnzbd select the typed Mullvad egress. Their source-based policy routes preserve RFC1918 and tailnet return paths, send MagicDNS only through `tailscale0`, and send other outbound traffic only through `wg-arr`; established-traffic guards plus a final per-TAP nftables drop form the kill switch if routing falls through. `nas-egress-mullvad.service` proves interface, route, DNS route, and firewall policy readiness, not a successful WireGuard handshake.
- The generated `nas-krun-network-policy.service` is the fail-closed readiness boundary for TAP guests. It publishes a current-boot marker only after networkd configures every active TAP and nftables exposes the required chains; TAP Quadlets wait for that marker and verify a guest TCP listener after startup so a missed one-shot DHCP exchange forces a VM restart. Stopping networkd or nftables removes readiness and stops the dedicated service user managers before the ruleset is flushed. Do not weaken this lifecycle ordering or start TAP guests based only on TAP existence.
- Disabled TOML services are excluded from TAPs, peer host entries, and nftables. TAP host publications deliberately accept only `127.0.0.1` (loopback-only) and `0.0.0.0` (all host addresses); add explicit policy semantics and namespace coverage before expanding that schema.
- Caddy uses 2 vCPUs and 512 MiB under libkrun. Root nftables publishes TCP 80/443, UDP 443, and loopback-only TCP 2019 to its guest; Caddy reaches backends by generated `*.krun` host entries and explicit inter-TAP allow rules. No Podman publisher, pasta, passt, or low-port host sysctl is involved. The krun handler still lacks `podman exec`, so configuration changes use service restarts.
- Jellyfin uses a 4-vCPU, 4-GiB libkrun guest on its dedicated TAP, with loopback-only host TCP 8096 DNAT behind Caddy. Its image healthcheck is disabled because it requires unsupported `podman exec`; blackbox probing is authoritative. The deployment deliberately omits `/dev/dri` hardware acceleration and UDP discovery; after the reviewed migration it mounts `/var/zfs/tank/videos/data/media` read-only without Podman `:z`/`:Z` relabeling.
- The four media-automation UIs have no host publications. Caddy reaches their declared guest endpoints and applies a shared source-IP guard permitting LAN private ranges plus Tailscale `100.64.0.0/10`, returning HTTP 403 to other sources.
- Immich uses four dedicated libkrun guests. The server alone is loopback-published on TCP 2283 behind Caddy; PostgreSQL, Valkey, and machine learning are reachable only over generated consumer edges. The server waits boundedly for PostgreSQL and Valkey, while machine learning remains independently restartable. See `docs/operations/immich.md` for first-use and recovery boundaries.
- The Jellyfin exporter polls the authenticated local `/Sessions` API and exposes only loopback Prometheus metrics on TCP 9594. It intentionally omits usernames and remote addresses; current titles remain metric labels because immediate playback diagnosis is the feature's purpose.
- Ordinary rootless crun with direct `/dev/dri/renderD128` proved that the host, Jellyfin image, permissions, and Intel media stack can expose VA-API codecs, but it is not an acceptable production fallback for this deployment because it removes the required VM boundary around Jellyfin and its plugins.
- For rootless Grafana, SELinux access is intended to come from persistent `semanage fcontext` rules plus `restorecon`, not from `SecurityLabelDisable=true`

## Build Performance

- **Previous**: 10+ minutes (ZFS compilation from source)
- **Current**: 2-3 minutes (prebuilt RPM consumption)
- **Improvement**: 70%+ reduction in build time

## Security Features

- **Encryption**: LUKS root filesystem with TPM2-based unlock, without PCR binding
- **Build Security**: Container image signing and attestations
- **Access Control**: SSH key-based authentication
- **Tailscale**: Daemon enabled (auth/config via runtime)

### Threat Model

This is a single-admin homelab NAS. The primary threats are:
- A malicious or compromised container image (e.g. a supply chain attack on Garage or VictoriaMetrics)
- Malware running on the host as an unprivileged user

We are **not** defending against: an attacker with root on the host (game over regardless) or a compromised container reading its own data (unavoidable).

### SELinux and Quadlet Containers

SELinux runs in enforcing mode (Fedora default). The main value for containers is **type enforcement**: files labeled `container_file_t` are only accessible to processes in the `container_t` domain, so host-level malware running as an unprivileged user cannot read container data. Mount namespaces provide the primary isolation between containers — each container only sees its explicitly declared volume mounts.

#### Volume labeling strategy

- **Small files on the root filesystem** (configs, secrets): use `:Z` (private MCS label) or `:z` (shared label) on the volume mount. Podman relabels these on every start, which is fine because they're tiny.
- **The same host file shared between containers**: use `:z` (shared). Using `:Z` causes the last container to start to steal the private label, breaking the other container. Separate per-service runtime-secret copies are not shared files and can each use `:Z`.
- **Large ZFS-backed data directories**: do **not** use `:Z` or `:z` on the volume mount. Podman's recursive SELinux relabeling runs on every container start and will hang or timeout on large directories. Instead, label these at dataset creation time using `semanage fcontext` (with `-r s0` to specify the MCS range) to set a persistent policy rule, then `restorecon -F -R` to apply it. The `-F` flag is critical — without it, `restorecon` only resets the SELinux type (e.g. `container_file_t`) but does **not** clear MCS categories (e.g. `s0:c148,c350` left behind by a previous `:Z` mount). The ZFS creation scripts check a sample file inside each directory on every boot and only run the full recursive relabel when the security-relevant type or MCS range is wrong; `system_u` versus `unconfined_u` alone is not drift.

#### ZFS snapshots and SELinux

SELinux labels are stored as xattrs on files. ZFS snapshots capture xattrs. Rolling back a snapshot restores old labels, which may not match the current policy. After any ZFS rollback, run `restorecon -F -R` on the affected mountpoints to reapply the `semanage fcontext` policy (the `-F` ensures the full context including MCS range is reset). The policy itself lives in the SELinux policy store on the root filesystem, not on the ZFS dataset, so it survives rollbacks. Same applies to `zfs send/receive` — the receiving machine needs its own `semanage fcontext` rules.

## Quick Start

- **Build the container image**: `make build`
- **Update the Ignition file** (after editing `butane.yaml`): `make generate-ignition`
- **Trigger CI build**: `make run-workflow`
- **Install Fedora CoreOS**: Use `https://samhclark.github.io/nas/ignition.json`

## Troubleshooting

**Build failures**: Check `make check-zfs-available` - likely no prebuilt ZFS kmods for current versions
**Workflow failures**: Check `make all-workflows` for status
**Ignition issues**: Verify with `make generate-ignition` locally first
