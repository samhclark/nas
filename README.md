# nas

A bootc container image for one personal NAS, published as a reference project
rather than a general-purpose appliance. The checked-in hostname, service mix,
storage layout, and secrets workflow are intentionally machine-specific.

## Published artifacts

- Image: `ghcr.io/samhclark/nas/bootc:stable`
- Ignition: <https://samhclark.github.io/nas/ignition.json>

The image is rebuilt daily with current Fedora CoreOS and compatible prebuilt
ZFS kernel modules. The Ignition document configures the host identity and
encrypted root storage; service configuration is carried by the image.

## Installation

Install Fedora CoreOS with the published Ignition URL, then switch to the NAS
image and reboot:

```text
https://samhclark.github.io/nas/ignition.json
```

The current profile configures a LUKS root filesystem with TPM2 unlock (without
PCR binding), Btrfs on `/dev/mapper/root`, SSH access for `core`, and hostname
`nas`. The profile is for the existing machine; inspect and adapt
[`butane.yaml`](butane.yaml) before using it elsewhere.

After installation, the remaining bootstrap is intentionally manual:

1. Switch and reboot into `ghcr.io/samhclark/nas/bootc:stable`.
2. Re-issue the switch with `--enforce-container-sigpolicy`, then reboot
   again so subsequent image pulls require the signed-image policy.
3. Log in to Tailscale and enable its SSH mode (`tailscale login` and
   `tailscale set --ssh`).
4. Install the SOPS age credential at
   `/var/lib/nas-secrets/age-key.cred`.
5. Enroll non-root encrypted volumes with TPM and add them to `crypttab`.
6. Import `tank` if it was not imported automatically.

The SOPS distributor decrypts the repository-managed file at
`/usr/share/nas/secrets/secrets.sops.yaml` during boot and writes per-service
runtime files below `/run/nas-secrets/`.

## Development

Required tools are GNU Make, Docker with Buildx, Podman, `gh`, `skopeo`, `jq`,
and `uv`.

```bash
make help                  # List targets
make check                 # Static contracts and generated parity
make test                  # Behavioral tests
make build                 # Build and verify the image locally
make publish               # Trigger the production image workflow
make generate-ignition    # Validate butane.yaml and render Ignition
```

Useful version and cleanup checks:

```bash
make versions
make check-zfs-available
make cleanup-dry-run RETENTION_DAYS=90
make workflow-status
make all-workflows
```

After changing `butane.yaml`, run `make generate-ignition`. After changing
`Containerfile` or `overlay-root/`, run `make build`. `make check` and `make
test` are separate canonical gates. The documented bootc lint warnings about
cache artifacts under `/var` can be ignored; unexpected `/var/usrlocal`
warnings usually indicate that content was copied before the image's immutable
`/usr/local` overlay took effect.

For rootless service changes, edit `quadlets/<service>.toml`, run
`make generate-quadlets`, and commit the generated files with the source. See
[`docs/development/rootless-quadlets.md`](docs/development/rootless-quadlets.md).

## Architecture

The build has six stages: a patched crun builder; verified SOPS, Vector runtime,
and Vector license inputs; prebuilt ZFS RPMs selected by the registry; and the
final Fedora CoreOS image. The authoritative package, unit, and overlay list
is in [`Containerfile`](Containerfile).

Compatibility is registry-based. A ZFS/kernel pair is accepted when the image
`ghcr.io/samhclark/fedora-zfs-kmods:zfs-X.X.X_kernel-Y.Y.Y` exists; otherwise
the build stops before image assembly. There is no hand-maintained matrix.

At runtime, the image provides ZFS, Tailscale, encrypted storage support, and
eighteen rootless Quadlet services under libkrun: ingress, observability,
Garage, Jellyfin, Immich's four components, and Sonarr, Radarr, Prowlarr, and
SABnzbd. The latter four media-automation services are deployed and validated
in production. See [`docs/README.md`](docs/README.md) for the authoritative
architecture and operations map.

Important boundaries:

- `quadlets/*.toml` is the source of truth for service identities, storage,
  endpoints, secrets, and generated network policy.
- Generated files with a `GENERATED` header must not be hand-edited.
- Large ZFS paths use persistent SELinux `fcontext` rules and `restorecon`, not
  recursive Podman `:z` or `:Z` relabeling.
- Rootless services consume per-service runtime secret files, not Podman
  `Secret=` objects.
- The production NAS is not an agent execution target; live commands are
  prepared for an operator to review and run.

## Security and release

Images are signed with Cosign and carry attestations. The public verification
key is [`overlay-root/etc/pki/cosign/cosign.pub`](overlay-root/etc/pki/cosign/cosign.pub),
and the image's policy is in `overlay-root/etc/containers/policy.json`.

GitHub Actions runs repository validation, SOPS verification, version and
compatibility resolution, image build/signing, and publication. The Pages
workflow renders the Ignition file; the cleanup workflow retains recent image
versions and defaults manual runs to dry-run mode.

## Further reading

- [`docs/architecture/`](docs/architecture/) — storage, secrets, networking,
  applications, and release boundaries
- [`docs/operations/`](docs/operations/) — current operator runbooks
- [`docs/roadmap.md`](docs/roadmap.md) — settled invariants and active work
- [`AGENTS.md`](AGENTS.md) — instructions for coding agents, including the
  production execution boundary
- [`fedora-zfs-kmods`](https://github.com/samhclark/fedora-zfs-kmods) — prebuilt
  ZFS kernel module source project
