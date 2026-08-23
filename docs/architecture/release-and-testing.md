# Release and testing architecture

## Canonical developer gates

The public Make interface intentionally keeps different questions separate:

- `make deps` verifies the development toolchain.
- `make check` performs static, non-mutating validation.
- `make test` runs behavioral tests.
- `make build` resolves current external inputs once and assembles the image.
- `make publish` explicitly triggers the production publishing workflow.
- `make all` gates the build behind deps, checks, and tests.

CI calls the same `check` and `test` commands through the reusable build
preflight workflow. Generated parity has a read-only compiler mode; validation
must never repair the working tree.

## Testing ladder

The testing and rollout layers increase in scope:

1. Local source contracts, schema validation, generated-contract parity,
   behavioral tests, and ordinary-container image startup smokes without
   production-state mutation. Image smokes exercise the full entrypoint,
   writable-volume behavior, and real readiness; add a libkrun-specific
   identity or runtime probe when that runtime can change the contract.
2. Exact built-image contract validation without booting it.
3. An explicit local QCOW2/QEMU boot smoke test with no guest network, host
   block device, production secret, storage preparation, or ZFS pool.
4. Operator-run production validation: first use, service restart, clean host
   reboot, external health observation, and data continuity.

For stateful services, include a post-population lifecycle check before the
production gate: prepare empty state, let or simulate the service creating
realistic descendants, then rerun preparation and readiness. The rerun must
publish current-boot readiness without repair and without storage mutations.
This is separate from an empty-state preparation test because container-root
or subordinate-ID descendants may only appear after first use.

Layers 1 and 2 are active. Every `make build` runs the exact-image contract
against the tag it just produced; `make verify-image` can repeat that read-only
contract without rebuilding.

The layer 3 runner is implemented as an opt-in local test for a separately
created, fresh QCOW2:

```console
make deps-vm
make test-vm QCOW=/absolute/path/to/fresh-image.qcow2
```

The runner does not create, convert, commit, rebase, or delete the supplied
image. It accepts only a regular, non-symlink, standalone QCOW2 with no backing
or external data file and passes a read-only integrity check. QEMU boots exactly
that one disk with `-snapshot`, a private temporary overlay, no NIC, and no host
filesystem passthrough. The runner checks the base image hash after shutdown.

The test Ignition masks service-user managers, updates, secrets, storage
preparation, and ZFS maintenance before testing bootc, SELinux, the ZFS module,
accounts, each per-user Quadlet, TAP creation, and nftables. Serial and QEMU
logs are retained under `build/vm-smoke/`; there is intentionally no cleanup
routine. The strict build-context allowlist and exact-image contract separately
prove that `tests/`, this fixture, and the host runner are not shipped.

This VM smoke is evidence for host boot contracts only. It is intentionally
storage-free and networkless, so it is not evidence for ZFS behavior,
service-created descendants, service readiness, or the full application
lifecycle. A production NAS reboot remains the final integration gate for
those properties.

The runner has passed behavioral fake-tool safety tests, strict Butane
validation, and a local QEMU capability probe, but an end-to-end guest pass is
not claimed yet because no fresh QCOW2 was available during implementation.
`make check` validates the test Ignition; actually booting the VM is not part of
`make check`, `make test`, `make build`, `make all`, or CI.

Automatic OCI-to-QCOW2 conversion is deliberately outside the current runner.
`bcvk` 0.18.0 is now installed on the development host and provides both
`to-disk --format=qcow2` and direct ephemeral boot with Ignition injection.
The first integration task is to use its to-disk path to supply a fresh image
to the existing runner, then compare its ephemeral path with the runner's
isolation and assertion contract. Do not add automatic conversion or replace
the runner until that end-to-end evidence exists; track this as R6 in the
[`roadmap`](../roadmap.md). The older `bootc-image-builder` path is
[deprecated upstream](https://osbuild.org/docs/bootc/deprecation-notice/).

A scratch ZFS-pool acceptance VM is deliberately deferred. No pool fixture or
teardown helper may be shipped in the image, and no test is allowed to target
the production NAS.

## Rollout evidence and closeout

Keep evidence proportional to the boundary being tested. Local gates establish
source, generated, image, and schema contracts; the storage-free/networkless VM
smoke establishes host boot contracts; only the operator-run NAS sequence
establishes the integrated stateful-service behavior. For each rollout, record
the image/source identity and the observed results for first use, restart,
clean reboot, external health, and data continuity. Archive the production
evidence in the history area when the rollout closes, then update the active
status and roadmap so they no longer describe the work as pending.

Service-specific operation documents remain the authority for exact commands
and recovery procedures. This document defines the evidence boundary and
ordering; it does not duplicate those commands or link active guidance to
archived history.

## Publishing decision

The scheduled publisher is serialized and never cancels an in-progress run.
One Buildx invocation exports the same build result to GHCR and the local
Docker image store. The workflow verifies the loaded image contract, then
attaches the signature and attestation to the published digest.

The `stable` tag therefore moves before contract verification, signing, and
attestation finish. The host's containers/image policy rejects an unsigned
`nas` image, so a build that fails verification remains unsigned and produces
a refused or delayed update rather than accepted bad content.
Candidate-to-stable promotion is deferred until there is an automated
candidate gate worth placing between build and promotion.

Revisit this decision if there is more than one consumer, if update retry
behavior changes, or when image/VM validation can run against the candidate
digest in CI.

## Production boundary

Agents never SSH to or execute on the NAS. Live evidence is collected only by
the operator using a reviewed, minimal copy-paste command. Test infrastructure
must make production targeting structurally impossible rather than relying on
a warning or teardown discipline.
