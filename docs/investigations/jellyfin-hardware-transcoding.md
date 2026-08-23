# Investigation: Jellyfin Hardware Transcoding Under libkrun

This is the working handoff for deciding whether Jellyfin hardware
transcoding can remain inside its libkrun microVM. The work is intentionally
incremental: each session should answer one question, record the evidence, and
leave production Jellyfin usable.

The active production choices are now:

1. keep Jellyfin under libkrun without hardware transcoding
2. continue the libkrun path through bounded external-kernel GPU experiments
3. evaluate a full KVM/QEMU guest with Intel SR-IOV or whole-device assignment
   if libkrun's mediated device path remains insufficient
4. use ordinary rootless crun with direct Intel render-node access only as a
   diagnostic control, not as an acceptable production deployment

A full libkrunfw forward-port and TSI/gvproxy production networking remain
possible research branches, but neither is justified until the GPU capability
probe succeeds.

## Current Status

Update this table and append a session-log row whenever new evidence changes
the handoff.

| Field | Value |
| --- | --- |
| Overall status | Linux 6.18 external-kernel hypothesis disproved; no production hardware acceleration configured |
| Last completed work | 2026-08-06: an image-contained Linux 6.18.42 kernel booted successfully with passt and Mesa 26.1.5, but repeated the rejected VirtIO-GPU commands and exposed only `VAEntrypointVideoProc` |
| Current phase | Add playback/transcode observability, measure the real requirement, and decide between another bounded libkrun experiment and a full KVM/QEMU GPU-assignment path |
| Recommended next experiment | Adapt only libkrunfw patch 0018 (virtio-gpu fence passing) to the existing external Linux 6.18 probe; stop if useful codecs still do not appear unless upstream identifies another specific small gap |
| Production runtime | Jellyfin remains rootless under libkrun with 4 vCPUs, 4 GiB RAM, private nested passt, loopback-only host TCP 8096, and software media processing |
| Production impact so far | None; no Jellyfin restart, host package change, or persistent configuration change was made during GPU probing |
| Primary blocker | The libkrun 1.19 / virglrenderer 1.3 / guest virtio-gpu native-context path does not expose Intel media codecs even with a guest kernel and Mesa newer than the documented minimums |
| Secondary blocker | Resolved for networking: crun's broad passt mapping is confined inside Jellyfin's private outer pasta namespace; only host-loopback TCP 8096 is published |

## Decision Snapshot

| Path | Current confidence | Effort and maintenance | Isolation | Position |
| --- | --- | --- | --- | --- |
| Keep software processing and favor Direct Play | Already operational | Low | Strongest | Accept if representative steady-state playback remains responsive |
| Rootless crun with direct `/dev/dri/renderD128` | High; direct VA-API probe passed | Low to moderate | Removes the required VM boundary and exposes the host Intel driver to Jellyfin and its plugins | Diagnostic control only; rejected for production by the operator |
| Full KVM/QEMU guest with an Intel SR-IOV VF or assigned iGPU | Unknown until host capability checks | High | Retains a full VM boundary | Investigate after playback observability; prefer an SR-IOV VF if the deployed firmware and kernel expose one |
| External Linux 6.18 plus one GPU patch | Uncertain | Moderate bounded experiment; potentially high if expanded | Retains microVM | Worth exactly one more discriminating probe |
| External kernel plus TSI or gvproxy | Depends on GPU success first | High | Retains microVM | Defer the networking choice |
| Replace or fully port libkrunfw | Uncertain | Highest effort and blast radius | Retains microVM | Do not pursue without strong upstream guidance |
| Report upstream and wait for a packaged fix | Uncertain timing | Low local maintenance | Retains microVM | Do in parallel with the bounded probe |

## Outcome and Decision Rule

Hardware transcoding under libkrun is worth keeping only if it can be made:

- reproducible from this repository
- isolated to Jellyfin rather than silently replacing firmware for every
  libkrun service
- compatible with the existing read-only media and persistent cache/config
  contracts
- observably useful for at least one real playback transcode
- maintainable without carrying a large, frequently rebased kernel fork

A disposable proof may be somewhat complicated. The production design should
not be. Stop an option when its ongoing maintenance cost is disproportionate
to one personal NAS.

## Evidence Already Collected

### Host and direct-container path

- The NAS has an Intel Alder Lake-N GPU (`8086:46d1`) driven by host `i915`.
- `/dev/dri/renderD128` is readable and writable by `_nas_jellyfin`.
- The official Jellyfin image successfully ran `vainfo` under ordinary crun
  using the Intel `iHD` driver. It advertised hardware decode and encode
  profiles, so the host, image, permissions, and Intel media driver are sound.
- Passing the same character device to a krun guest did not work. A host DRM
  character-device node is not direct PCI passthrough into the microVM.

### VirGL video path

- Fedora 44 packages `libkrun` with GPU support and `virglrenderer` 1.3.0 with
  its video feature enabled.
- Enabling only `VIRGL_RENDERER_USE_VIDEO` (`krun.gpu_flags=2048`) caused the
  libkrun VMM to exit with status 139 before the guest started.
- libkrun 1.19's bundled rutabaga VirGL callbacks do not provide the complete
  GL/DRM callback setup used by the VirGL video path. Treat the crash as a
  runtime implementation limitation, not as evidence that the Intel GPU is
  broken.

### DRM native-context path

- The upstream crun native-context mask `1411` combines `USE_EGL`,
  `THREAD_SYNC`, `NO_VIRGL`, `ASYNC_FENCE_CB`, and `DRM`.
- With that mask, the microVM booted and a Fedora 44 guest installed Mesa
  26.1.5 and loaded `virtio_gpu_drv_video.so`.
- The guest kernel was Linux 6.12.91 from libkrunfw 5.5.0. The host kernel was
  Linux 7.1.4.
- `vainfo` initialized but advertised only `VAEntrypointVideoProc`. It exposed
  no `VLD`, `EncSlice`, or `EncSliceLP` entrypoints and logged rejected
  VirtIO-GPU commands. This is not usable hardware transcoding.
- Current virtio-gpu documentation requires Linux 6.14 or newer plus Mesa
  26.1 or newer for an Intel i915 guest native context. Mesa met the threshold;
  the bundled guest kernel did not.

### External Linux 6.18 result

- A Fedora 44 probe image successfully selected an image-contained Linux
  6.18.42 ELF kernel through `/.krun_vm.json`.
- The guest booted, mounted its OCI root filesystem, saw the mediated
  virtio-gpu render node, used standard virtio-net through passt, ran the
  probe, and exited cleanly. This proves that an external kernel can be scoped
  to Jellyfin without replacing the host's packaged libkrunfw.
- passt was run inside a temporary host network namespace, so crun's broad
  forwarding behavior could not bind production host ports.
- Mesa 26.1.5 loaded `virtio_gpu_drv_video.so`, but the guest again logged
  VirtIO-GPU `0x1200` error responses, identified the renderer as generic
  `virgl`, and exposed only `VAEntrypointVideoProc`.
- Linux 6.18.42 and Mesa 26.1.5 exceed the documented Intel native-context
  guest minimums. Therefore, guest kernel age alone was not the blocker. The
  remaining mismatch may be in the guest's out-of-tree virtio-gpu protocol
  support, libkrun's device implementation, virglrenderer/native-context
  setup, or their combination.

### libkrun `GPU=1` build audit

- smolvm correctly notes that GPU acceleration requires libkrun to be built
  with `GPU=1`. Upstream's Makefile translates that setting into the Rust
  `gpu` feature.
- Fedora 44 enables its `gpu` build condition by default, installs the
  virglrenderer development dependency, and invokes libkrun's build with
  `GPU=1`.
- The NAS probe called `krun_has_feature(KRUN_FEATURE_GPU)` against the
  installed library and received `1`, which is the runtime confirmation that
  this exact libkrun binary contains GPU support.
- The VMM also created a mediated virtio-gpu device and reached the guest Mesa
  driver. A libkrun build without the GPU feature would fail before this point.
- smolvm documents a Venus/Vulkan workload. Jellyfin needs VA-API media
  decode/encode through Intel DRM native context. `GPU=1` is required for both
  families of paths, but it does not guarantee that every backend and guest
  media API is implemented successfully.
- Do not rebuild libkrun merely to add `GPU=1`; that would reproduce a feature
  already present in the installed Fedora library and would not discriminate
  the current failure.

### libkrunfw 6.18 feasibility

- libkrunfw 5.5.0 and its current upstream branch both embed Linux 6.12.91.
- Its build applies 30 kernel patches. A dry-run against Linux 6.18.42 failed
  on the first patch, so changing the version constant is not sufficient.
- Many patches are architecture-specific or unrelated to this Jellyfin probe.
  A Jellyfin-specific external kernel using standard virtio-net/passt may not
  need the TSI patch series or the entire firmware patch stack.
- The libkrunfw stack includes a separate 152-line virtio-gpu fence-passing
  patch (0018). It does not apply cleanly to Linux 6.18 without adaptation.
  It is a plausible missing protocol component, not a known fix: its stated
  purpose is sharing and waiting on host/guest fences, and the current evidence
  does not prove that fence passing caused context initialization to fall back.
- TSI is not a kernel configuration option. On x86_64 it depends on the
  libkrunfw vsock-datagram and socket-impersonation series, approximately
  patches 0003 through 0012. Several do not apply cleanly to Linux 6.18, and
  the main TSI implementation alone adds roughly 1,700 lines.

## Recommended Order of Work

Use this order unless new evidence changes it:

1. Let the initial media scan finish and establish whether representative
   clients actually require video transcoding. Record Direct Play, Direct
   Stream, audio transcode, subtitle burn-in, or full video transcode rather
   than inferring the workload from host load.
2. Prepare a concise upstream libkrun report containing both guest-kernel
   results, exact host/runtime/Mesa versions, GPU flags, and the rejected
   VirtIO-GPU commands. Ask whether Intel VA-API native context is expected on
   this stack and whether patch 0018 or another downstream component is
   required.
3. If one more local experiment is worthwhile, adapt only patch 0018 to the
   existing external Linux 6.18 probe. Do not add Jellyfin, TSI, gvproxy,
   storage, or production configuration to this test.
4. Apply a firm stop rule: if the patched probe still exposes only
   `VAEntrypointVideoProc`, stop private microVM GPU work unless upstream names
   another specific and bounded missing component.
5. If the probe succeeds, require a real ffmpeg VA-API operation before
   solving production networking. If it fails, choose between software
   processing and a full KVM/QEMU GPU-assignment path; do not treat the
   already-proven ordinary rootless-crun path as production-approved.
6. Do not begin a full libkrunfw or TSI forward-port solely because a custom
   kernel can be built. Make the final choice using observed playback benefit
   and long-term maintenance cost.

## Deferred Option: Forward-Port libkrunfw to Linux 6.18

### What it means

Build a replacement libkrunfw containing a Linux 6.18 LTS kernel and rebase,
drop, or replace the upstream 6.12-oriented patch series. libkrun would then
use the newer bundled kernel by default.

### Why it is attractive

- All existing krun behavior remains expressed through libkrunfw's normal
  firmware mechanism.
- TSI could remain available, preserving Jellyfin's current loopback-only TCP
  design and avoiding passt's broad port forwarding.
- If accepted upstream, the maintenance burden could eventually disappear.

### Why it is risky

- Replacing the normal libkrunfw changes the guest kernel for Caddy, Garage,
  Grafana, VictoriaMetrics, Alertmanager, vmalert, blackbox exporter, and
  Jellyfin—not just Jellyfin.
- The 30-patch stack does not forward-apply. Some patches may already be
  upstream, some need rebasing, and some are irrelevant on x86_64, but this
  requires kernel-level review rather than mechanical conflict resolution.
- A private firmware build becomes a security- and update-maintenance
  obligation.
- The external-kernel probe showed that Linux 6.18 by itself does not expose
  media codecs, so a full firmware port no longer has a demonstrated payoff.

### Safer experimental shape

Before replacing the packaged firmware, investigate whether a separate
libkrunfw build can be selected only by a disposable runtime wrapper or an
isolated `LD_LIBRARY_PATH`. Do not install a same-soname replacement on the
production host merely to run the first test.

### Phases and gates

1. Classify all 30 patches for Linux 6.18:
   already upstream, required on x86_64, required only for TSI, optional, or
   needs a real rebase.
2. Build and boot a disposable firmware artifact outside production.
3. Re-run a minimal krun process with no storage mounts.
4. Re-run the native-context VA-API probe and require at least one decode or
   encode entrypoint.
5. Stop unless the replacement can be selected per experiment or every active
   krun service has a deliberate regression plan.

### Completion criteria

Option 1 is viable only if the new firmware boots all required libkrun
features, exposes useful Intel VA-API capabilities, and has an acceptable
upgrade story. A one-off successful kernel build is not enough.

## Research Option: External Jellyfin Kernel

### Short answer on complexity

This is **easy enough to try, but not an easy production toggle**.

The disposable infrastructure proof is complete: the image-contained kernel,
OCI root, passt network, mediated GPU device, and clean process lifecycle all
worked. It is much safer than replacing libkrunfw because the packaged
firmware and the other seven services remain untouched.

The GPU capability gate failed with a stock Linux 6.18.42 kernel. Continuing
now means adapting a missing out-of-tree GPU component or changing the VMM /
virglrenderer side rather than merely choosing a newer kernel.

Productionizing it would remain moderately high complexity if that gate later
succeeds. The kernel and guest Mesa must be built and updated reproducibly,
Jellyfin needs a derived image, the Quadlet generator needs new fields, and
crun 1.28's all-port passt behavior must be resolved.

### Proposed architecture

```text
Jellyfin-derived OCI image
├── official Jellyfin application and ffmpeg
├── Mesa VirtIO VA-API driver, version 26.1 or newer
├── /usr/lib/jellyfin-krun/vmlinux (Linux 6.18 LTS or newer)
└── /.krun_vm.json (selects the ELF external kernel)

rootless Podman / crun-krun
├── krun.cpus=4
├── krun.ram_mib=4096
├── krun.gpu_flags=1411
├── krun.use_passt=1
└── AddDevice=/dev/dri/renderD128

libkrun
├── loads the image-contained kernel instead of libkrunfw's 6.12 kernel
├── exposes virtio-gpu DRM native context
├── uses standard virtio-net through passt
└── keeps config, cache, and media on the existing virtiofs mounts
```

The external-kernel path semantics are now proven by the NAS probe. crun 1.28
read `/.krun_vm.json` from the image and loaded the specified x86_64 ELF
`vmlinux` using kernel format `1`.

### Why passt is part of this option

The stock Linux kernel does not contain libkrun's TSI socket-hijacking patch
series. Standard virtio-net is available, so passt supplies guest networking
without requiring those patches. This sharply reduces the kernel-forward-port
work needed for the proof.

crun 1.28 starts passt with all TCP and UDP ports, but the 2026-08-06 nested
network tests corrected the scope of that behavior: with Podman's normal
rootless pasta network, those listeners exist only inside that container's
private namespace. Two guests concurrently used the same guest port without a
host collision, while outer `PublishPort` exposed only the selected loopback
port. Passt is therefore approved for this topology; host networking remains
explicitly incompatible.

### gvproxy as the production networking variant

libkrun 1.19 already has a gvproxy backend through its Unix datagram network
API. That makes gvproxy a credible way to avoid passt's broad port behavior,
especially because this repository is willing to own a derived application
image. It is not, however, a drop-in Quadlet annotation with the currently
installed stack: crun 1.28's krun handler exposes `krun.use_passt`, starts
passt itself, and does not expose or supervise libkrun's gvproxy API.

If a future patched GPU probe succeeds, evaluate these implementations in order:

1. add a narrow crun krun annotation for a pre-opened or named gvproxy
   Unix-datagram endpoint and keep normal Podman/Quadlet lifecycle management
2. if upstream crun has gained equivalent support, rebase onto that instead
3. use a separately supervised gvproxy plus purpose-built launcher only if the
   crun extension is disproportionate

Whichever form is chosen must publish only Jellyfin's loopback TCP 8096,
preserve Caddy's current route, terminate cleanly with the user service, and
avoid owning unrelated TCP or UDP ports. Owning a derived Jellyfin image does
not by itself solve this host-side lifecycle integration.

### Phase 2A: External-kernel boot smoke test

Status: **passed on the NAS on 2026-08-06**.

Goal: prove that crun/libkrun can boot an image-contained Linux 6.18 kernel
without changing host firmware.

1. Pin a Linux 6.18 LTS patch release.
2. Start from libkrunfw's x86_64 kernel configuration, then run
   `olddefconfig` against 6.18.
3. Ensure the built-in configuration includes the boot-critical pieces used
   by libkrun: KVM guest support, virtio-mmio, virtio console, virtiofs/FUSE,
   virtio-net, DRM virtio-gpu, resource blobs, and context initialization.
4. Do not apply the TSI patch series for this proof.
5. Build an uncompressed ELF `vmlinux` and a minimal Fedora 44 probe image
   containing it plus `/.krun_vm.json`.
6. Run it with passt in an isolated network namespace and execute only
   kernel, filesystem, device, and VA-API checks. Outbound connectivity is not
   required for the first GPU discriminator because the image is self-contained.

Gate: stop and diagnose if the guest does not report the external 6.18 kernel,
cannot mount its OCI root through virtiofs, or cannot exit cleanly.

### Phase 2B: Native-context GPU capability test

Status: **failed with both Linux 6.12.91 and external Linux 6.18.42**. Both
loaded the Mesa VA driver but exposed only video processing.

Goal: determine whether the newer guest kernel changes the result from
video-processing-only to useful media codecs.

1. Add Fedora Mesa 26.1-or-newer VA tooling to the disposable probe image.
2. Add the host render node to the OCI specification.
3. Use `krun.gpu_flags=1411`.
4. Work around crun 1.28's unconditional
   `/usr/libexec/virgl_render_server` pathname check without enabling the
   render-server bit. Prefer a newer packaged crun containing the conditional
   check if one becomes available.
5. Run `vainfo` with `LIBVA_DRIVER_NAME=virtio_gpu`.

Gate: success requires at least one real decode or encode entrypoint. Merely
loading the driver, returning exit status zero, or advertising
`VAEntrypointVideoProc` does not count.

Record all of:

- guest kernel and Mesa versions
- host kernel, libkrun, crun, and virglrenderer versions
- complete `vainfo` profile/entrypoint list
- guest kernel VirtIO-GPU errors
- whether the driver identifies a native Intel context or falls back to
  generic VirGL

### Phase 2B.1: Bounded fence-passing discriminator

This is the only recommended private-kernel follow-up before seeking upstream
direction.

1. Adapt only libkrunfw patch 0018 to Linux 6.18.42.
2. Preserve the exact image, Mesa version, GPU flags, isolated passt runner,
   and success criterion from Phase 2B.
3. Record the guest's negotiated virtio-gpu features and complete kernel log
   around device initialization in addition to `vainfo`.
4. Stop if the result remains `VideoProc`-only. Do not respond by beginning a
   broad patch-stack port without new causal evidence.

This patch is a hypothesis, not a promised fix. Its upstream description is
about passing and sharing fences across the guest/host boundary; that is
relevant to native contexts but does not by itself prove why initialization
currently falls back to generic VirGL.

### Phase 2C: Exact Jellyfin userspace test

Only begin after Phase 2B passes.

1. Derive an image from the pinned official Jellyfin image.
2. Add the external kernel and `/.krun_vm.json`.
3. Add a Mesa VirtIO VA-API driver new enough for Intel native context.
   Verify the Debian/Trixie package version rather than assuming the package
   name is sufficient.
4. Keep the official Jellyfin ffmpeg build.
5. Run an ffmpeg VA-API decode/encode test against a small synthetic or
   non-sensitive fixture before mounting the media library.
6. Confirm host GPU activity and materially lower guest CPU use.

Gate: do not touch production Jellyfin configuration until ffmpeg performs a
real hardware-backed operation successfully.

### Phase 2D: Repository integration

Only begin after the exact-image test passes.

Likely repository work:

- add a reproducible derived Jellyfin image build and pin its resulting digest
- record the kernel source version, configuration, patches, and corresponding
  source-distribution obligations
- extend `[krun]` in `generate-quadlets.py` with a narrowly validated field for
  `gpu-flags`; the typed `network = "passt"` field is already implemented
- add generated `Annotation=krun.gpu_flags=1411` and
  `Annotation=krun.use_passt=1`
- add `AddDevice=/dev/dri/renderD128`
- add generator validation and tests; do not use arbitrary unvalidated
  `PodmanArgs=` when a typed field is reasonable
- preserve all existing Jellyfin storage mounts, ownership, SELinux labeling,
  resource limits, Caddy route, and graceful shutdown behavior

The passt networking gate is complete on the real single-host topology. A
future GPU integration still requires its own device and codec gates.

### Phase 2E: Production validation

Use a brief planned Jellyfin outage and retain a rollback image/config.

Validate in this order:

1. service startup, guest kernel version, and exact pinned image
2. config/cache/media mount contracts and read-only media access
3. Caddy route and loopback exposure
4. Jellyfin VA-API configuration using `/dev/dri/renderD128`
5. a known forced video transcode with subtitles initially disabled
6. host GPU utilization, ffmpeg command line, guest/host CPU, memory, playback
   stability, and Grafana responsiveness
7. graceful restart and recovery after a host reboot

Rollback immediately if the VA driver exposes no useful codec, unrelated host
ports appear, or playback becomes less reliable.

### External-kernel maintenance cost

If deployed, this repository would own three additional version relationships:

- Jellyfin version to its derived image
- guest kernel version/configuration to libkrun/crun behavior
- guest Mesa version to virglrenderer/native-context behavior

That is acceptable only if hardware transcoding provides a clear operational
benefit. Prefer consuming an upstream libkrunfw/crun fix later and deleting the
custom path.

## Production Option: Keep Software Processing

This is a valid success state, not a failed experiment.

### First establish the real workload

The initial observation happened while Jellyfin was scanning the library,
extracting many subtitle tracks, fetching metadata, and serving playback. The
logs did not yet show a definite video-transcoding ffmpeg command. Before
concluding that software transcoding is unacceptable:

1. let the initial scan and subtitle extraction finish
2. play representative media on the actual clients
3. inspect Jellyfin's playback information for Direct Play, Direct Stream,
   audio transcode, subtitle burn-in, or video transcode
4. capture the corresponding ffmpeg command and CPU/memory use
5. test with graphical subtitles disabled to distinguish subtitle burn-in
   cost from codec transcoding

### Possible steady-state adjustments

- prefer Direct Play/Direct Stream profiles on trusted clients
- avoid forced PGS subtitle burn-in where text subtitles are available
- pre-extract or convert subtitle formats if that matches the library
- schedule library scans and expensive maintenance away from viewing time
- revisit the 4-vCPU/4-GiB allocation only after measuring steady state
- accept software transcoding for rare cases if normal playback remains
  responsive

### Completion criteria

Choose this option if normal household playback is predominantly direct,
occasional software processing stays responsive, and the kernel/image
maintenance required by the custom-kernel paths is not justified.

## Diagnostic Control Only: Rootless crun

Jellyfin alone could move from krun to ordinary rootless crun and receive
`/dev/dri/renderD128`. The disposable crun probe already proved that the
official Jellyfin image can initialize the Intel `iHD` VA-API driver this way.

This would sacrifice Jellyfin's microVM boundary but retain the rootless
service identity, explicit mounts, SELinux enforcement, and direct hardware
transcoding. A compromised Jellyfin process would gain the ability to issue
DRM ioctls directly to the host Intel driver, so this is a meaningful increase
in host attack surface under the repository's threat model. It is nonetheless
the highest-confidence and lowest-maintenance hardware-transcoding option
currently available, scoped to one hardware-oriented service.

This path is retained only as a diagnostic control proving the physical GPU,
permissions, image, and Intel media userspace. The operator requires a VM
boundary around Jellyfin and its plugins, so representative playback pressure
must not turn this successful probe into an implicit production migration.

## Safety and Working Rules

- Agents must not SSH to or execute commands on the production NAS. Put the
  smallest reviewed operator command in a temporary file and wait for returned
  output.
- Do not stop or reconfigure production Jellyfin for Phases 2A through 2C.
- Run GPU/kernel probes without config, cache, or media mounts.
- Keep state-changing NAS commands separate from evidence collection.
- Do not replace the host's packaged libkrunfw during an exploratory session.
- Do not hand-edit generated Quadlets. Change `quadlets/jellyfin.toml` and the
  generator, regenerate, and commit both only after the experiment passes.
- Pin every image, kernel source archive, and derived artifact used in a
  reproducible result.
- Treat a successful `vainfo` exit status as insufficient; require useful
  codec entrypoints and then a real ffmpeg hardware operation.
- Remove or explicitly record disposable images and probe artifacts after each
  NAS session.

## References

- [Jellyfin Intel hardware acceleration](https://jellyfin.org/docs/general/post-install/transcoding/hardware-acceleration/intel/)
- [QEMU virtio-gpu, VirGL, Venus, and DRM native context](https://www.qemu.org/docs/master/system/devices/virtio/virtio-gpu.html)
- [libkrun source and GPU model](https://github.com/containers/libkrun)
- [libkrun 1.19 build-time GPU feature](https://github.com/libkrun/libkrun/blob/v1.19.0/Makefile)
- [crun krun annotations](https://github.com/containers/crun/blob/1.28/krun.1.md)
- [libkrunfw 5.5.0 kernel build](https://github.com/containers/libkrunfw/blob/v5.5.0/Makefile)
- [libkrunfw virtio-gpu fence-passing patch](https://github.com/containers/libkrunfw/blob/v5.5.0/patches/0018-drm-virtio-Support-fence-passing-feature.patch)
- [Linux kernel releases](https://www.kernel.org/)
- [Fedora 44 libkrun build configuration](https://src.fedoraproject.org/rpms/libkrun/raw/f44/f/libkrun.spec)
- [Fedora 44 virglrenderer build configuration](https://src.fedoraproject.org/rpms/virglrenderer/raw/f44/f/virglrenderer.spec)
- [smolvm GPU requirements and Venus example](https://github.com/smol-machines/smolvm#known-limitations)
- [Podman 5.8.1 Quadlet `AddDevice=` reference](https://docs.podman.io/en/v5.8.1/markdown/podman-systemd.unit.5.html#adddevice)

## Session Log

| Date | Session | Evidence / decision | Production impact | Next action |
| --- | --- | --- | --- | --- |
| 2026-08-06 | Host and direct-device baseline | Intel iHD VA-API succeeded under crun; direct render-node use failed inside krun | None | Investigate mediated virtio-gpu paths |
| 2026-08-06 | VirGL video probe | `krun.gpu_flags=2048` crashed the VMM with exit status 139 before guest startup | None | Avoid VirGL video on libkrun 1.19; test DRM native context |
| 2026-08-06 | DRM native-context probe | Mask 1411 booted Fedora 44 with Mesa 26.1.5, but libkrunfw's 6.12.91 guest exposed only `VAEntrypointVideoProc` and rejected VirtIO-GPU commands | None; disposable Fedora image remained cached in `_nas_jellyfin`'s rootless store | Test a guest kernel new enough for Intel native context |
| 2026-08-06 | libkrunfw 6.18 patch dry-run | The upstream 30-patch 6.12 series failed to apply to Linux 6.18.42 at patch 0001 | None; local temporary source trees only | Prefer option 2 external-kernel smoke test before rebasing full firmware |
| 2026-08-06 | External-kernel prototype assembly | Added a repeatable Fedora 44 probe that builds verified Linux 6.18.42 sources from libkrunfw 5.5.0's config, selects its ELF kernel through `/.krun_vm.json`, and exports an operator-loadable OCI archive; local validation confirmed the 28.2-MiB kernel, boot-critical built-ins, Mesa 26.1.5 driver, and a 159-MiB archive; the NAS runner isolates passt in a temporary host network namespace | None; repository-local files and a local build only | Run the operator-mediated NAS probe |
| 2026-08-06 | External Linux 6.18 NAS probe | Linux 6.18.42 booted successfully with the OCI root, mediated render node, Mesa 26.1.5, and isolated passt, but repeated VirtIO-GPU `0x1200` errors and exposed only `VAEntrypointVideoProc`; kernel age alone is not the blocker | None; production Jellyfin remained running, and only a disposable image was imported into `_nas_jellyfin`'s rootless store | Audit the host libkrun GPU build and consider one bounded patch-0018 probe |
| 2026-08-06 | libkrun GPU build audit | Fedora 44's spec enables `GPU=1` by default, and the installed library returned `1` for `krun_has_feature(KRUN_FEATURE_GPU)`; smolvm's build-time warning is satisfied and does not explain the VA-API failure | None; read-only source/spec inspection | Measure production transcoding, prepare an upstream report, and use patch 0018 as the final bounded private-kernel discriminator |

When resuming, append one row that records the exact artifact versions, the
single question answered, whether anything reached production, and the next
smallest discriminating action.
