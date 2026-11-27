# AGENTS.md

This repository builds the bootc image and host configuration for one personal NAS. It is an open source reference, not a general-purpose appliance. The production image is `ghcr.io/samhclark/nas/bootc:stable`; Ignition is served at `https://samhclark.github.io/nas/ignition.json`.

## Orientation and configuration boundaries

- `Makefile` and `.github/workflows/` choose versions and build, validate, and publish artifacts. `Containerfile` defines the image; `overlay-root/` defines runtime behavior. Prefer these files and `butane.yaml` over prose when determining actual behavior.
- `butane.yaml` is intentionally personal and limited to host identity and install-time LUKS/Btrfs setup, not service orchestration. Ignition and the image are independent artifacts with separate workflows.
- Treat `/usr` as immutable and `/var` as persistent runtime state. Put static assets under `/usr/share/nas`; seed runtime state with tmpfiles or `StateDirectory=`. This image deliberately makes `/usr/local` an immutable image-managed directory rather than the Fedora CoreOS default symlink.
- ZFS/kernel compatibility is registry-based: the matching `ghcr.io/samhclark/fedora-zfs-kmods:zfs-X.X.X_kernel-Y.Y.Y` image must exist. The related `../fedora-zfs-kmods/` project publishes these modules.
- The root filesystem is TPM-unlocked LUKS and Btrfs without PCR binding; the main ZFS pool is `tank`. Manual enrollment of non-root LUKS volumes, installation of the SOPS age credential, and sometimes pool import remain intentional bootstrap steps.

## NAS execution boundary

Agents must not SSH to or execute commands on the production NAS. When NAS evidence or an operational action is needed, prepare a reviewed copy-paste command for the operator, explain its effects, and wait for the output. Prefer the smallest command that answers the question; keep state-changing commands separate and explain them before asking the operator to run them.

Changes committed and pushed to `main` are picked up by the scheduled image build and NAS update. In a later session, assume such changes are deployed unless the operator or current evidence says otherwise; do not add a manual publish or deployment step by default.

## Generation and verification

- Rootless service definitions live in `quadlets/*.toml`. Edit those sources, run `make generate-quadlets`, and include generated changes. Never hand-edit files with a `GENERATED` header, including generated network and nftables files. The generated manifests drive account, storage, secret, asset, and TAP provisioning.
- Run `make check` for static contracts and generated parity and `make test` for behavioral coverage. These are separate canonical gates.
- After changing `butane.yaml`, run `make generate-ignition`. After changing `Containerfile` or `overlay-root/`, run `make build`.
- `make versions`, `make check-zfs-available`, and `make deps` help diagnose version and build prerequisites. `make help` lists other commands; `make publish` and workflow targets trigger remote actions.
- `bootc container lint` warnings about `/var` cache artifacts are currently expected. `/var/usrlocal` warnings usually indicate something was copied into `/usr/local` before the overlay replaced the default symlink.

## Runtime invariants

- Services use rootless admin-managed user Quadlets under `/etc/containers/systemd/users/${UID}/`, libkrun VM isolation, and root-managed routed TAPs. Preserve the generated fail-closed network policy lifecycle: guests wait for current-boot policy readiness and a listener check, and stopping networkd or nftables stops their user managers before rules are flushed. TAP guests require `/dev/net/tun`. Do not replace Jellyfin's VM boundary with ordinary crun to obtain hardware transcoding.
- Media automation uses typed Mullvad egress with a routing and nftables kill switch. Preserve private-network and tailnet return paths, MagicDNS routing, and the final per-TAP drop. Egress readiness does not prove a WireGuard handshake. See [libkrun networking](docs/architecture/libkrun-networking.md).
- Stateful storage, mounts, SELinux policy, and readiness are declared in `[[storage]]` in `quadlets/*.toml`; raw `/var` container volumes are rejected. Respect required-existing and preserve-owner declarations. Shared media roots under `tank/videos` are root-owned, group-owned by media GID `52000`, and mode `2775`; the shared-storage unit validates them without automatically creating, moving, changing ownership, or relabeling production media. See [storage architecture](docs/architecture/storage.md).
- SELinux is enforcing. Use persistent `semanage fcontext` and `restorecon -F -R` for large ZFS data paths, not recursive Podman `:z`/`:Z` relabeling on each start. Small shared files use `:z`; separate private files may use `:Z`. After a ZFS rollback, restore labels on affected mountpoints; receiving hosts need their own policy. Do not disable SELinux labeling to fix access.
- SOPS-encrypted secrets are distributed at boot by the root-owned `sops-distribute-secrets.service` into per-service `/run/nas-secrets/` files, mounted read-only with `:ro,Z`. Do not use rootless Podman `Secret=`. The Mullvad key is delivered to networkd as a service credential, never rendered into a `.netdev`. See [secrets architecture](docs/architecture/secrets.md).
- Avoid cross-manager systemd dependencies from user services to system units; use independent startup or bounded readiness checks. Garage availability uses the blackbox probe of `http://garage.krun:3903/health`, since its metrics scrape can respond slowly.
- The threat model includes compromised container images and unprivileged host malware, but not a host-root attacker or a container reading its own data. Preserve mount and SELinux isolation.

## Service identities

Use namespaced host usernames such as `_nas_grafana` and reserve `51000–51999` for image-managed accounts: `511xx` storage/application, `512xx` observability, `513xx` ingress, and `514xx` media automation. `quadlets/*.toml` is the registry of active allocations. UIDs are allocate-only: never reuse a retired UID, since numeric ownership persists in files and snapshots. When the first service is retired, record its UID here and add a `retired-uids` check to `generate-quadlets.py`.

Subordinate IDs are a separate, globally coordinated allocator. Keep ranges non-overlapping; the convention derives a 65536-wide range from the host UID, e.g. `_nas_grafana:512100000:65536`. Generated storage manifests must retain the ownership contract: owned roots require the exact service UID/GID, while descendants may also use declared supplemental fleet GIDs or the service's subordinate range.

## Further reading

Start with the [documentation index](docs/README.md), [rootless Quadlet development guide](docs/development/rootless-quadlets.md), and [release and testing architecture](docs/architecture/release-and-testing.md). Service-specific operational and investigation documents are linked from the index.
