# Local Immich backup VM smoke

The opt-in `make smoke-immich-backup-vm` target exercises the first local
integration boundary for `nas-backup-immich` against Docker's exact local
`nas/bootc:stable` build tag. The runner imports that exact Docker image into
rootless `containers-storage` and compares image IDs before bcvk sees it; it
never defaults to a registry image. It uses `bcvk ephemeral run` with a
retained disposable scratch disk, creates a temporary ZFS `tank` pool in the
guest, and populates only synthetic Immich library data.

The guest has no network (`--network=none`) and receives no production secret,
host filesystem, block device, or SOPS material. The pinned restic image and
synthetic fixture script are copied into a read-only bcvk virtiofs bind before
boot. The guest loads that image locally, so the local backup and restore path
does not pull from a registry. bcvk's execute channel runs the fixture and
returns its status; no first-boot Ignition or manufactured boot disk is
involved.

The ephemeral guest enables SELinux in permissive mode. `bcvk` deliberately
disables SELinux for its virtiofs-backed ephemeral root, and forcing enforcing
mode prevents that test-only root from completing switch-root before the
fixture can run. Permissive mode still loads policy, applies and verifies the
production `container_file_t:s0` labels, and records would-be denials. Static
storage-policy tests and the operator-run NAS gate retain responsibility for
proving host enforcement; this fixture does not claim that boundary.

The fixture intentionally omits the host-VM TAP manifest. `init` must create
and structurally check the repository before failing closed at the outbound
boundary. `run` must then back up and validate the local recovery point before
failing at that same boundary. The fixture verifies the nonempty snapshot
summary, local-success and failed-run metrics, unchanged remote-success state,
deletion of the staging snapshot, restored file content and numeric ownership,
and a user xattr. Its no-network restore guest receives only the capabilities
needed to restore ownership and metadata; it excludes the captured SELinux
xattr because the host recovery procedure reapplies host policy with
`restorecon`.

Artifacts are retained under `build/immich-backup-vm-smoke/run.*`; the runner
does not delete them. The scratch disk is hash-checked before and after the
VM and must change, proving the guest attached the disposable storage; it is
never a production device.

## Dependencies and use

The host needs Docker, Podman, Skopeo, `bcvk` 0.18 or newer, and `qemu-img`.
`/dev/kvm` must be readable and writable. The exact local Docker image must
already exist:

```console
docker image inspect nas/bootc:stable
make smoke-immich-backup-vm
```

If the pinned restic image is absent, the runner pulls that public fixture
dependency on the host and records it in the retained artifact directory. No
guest network is ever enabled. Nested KVM is required because the guest runs
the same rootful Podman/libkrun restic VM that production uses; a failure to
expose `/dev/kvm` is a test failure, not a reason to fall back to ordinary
crun.

This phase does not test B2, rclone, host TAP DHCP/NAT, real NAS storage, or a
production reboot. Those require a later disposable network fixture and the
operator-run NAS gate respectively.
