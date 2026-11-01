# SABnzbd network receive stall, 2026-10-31

## Status and decision

Investigation deferred at the operator's request on 2026-10-31 local time
(2026-11-01 UTC). SABnzbd recovered after an operator-run service restart.
No upstream issue, pull request, fork, or runtime patch was created. The
attempt to clone an upstream working checkout was interrupted before that
checkout existed. No further upstream work is authorized by this note.

The operator does not want to submit an agent-generated patch to a critical
dependency without understanding and being able to stand behind it. Revisit
this only as deliberate maintenance work, not as an assumed next task.

**Confirmed:** SABnzbd's microVM stopped responding to inbound network
traffic while the application and some guest-origin traffic continued.
Restarting only SABnzbd restored connectivity and downloads.

**Unconfirmed:** a receive-notification hazard identified in libkrun caused
this particular outage. A small local experiment demonstrates notification
suppression for the suspected ordering, but does not reproduce the complete
network stall. Earlier conversational descriptions of a concrete defect or
root cause should be read with that limitation.

## Timeline and recovery

Times below are host journal timestamps in UTC. Application timestamps in
the old guest lagged host journal timestamps by roughly 24 seconds, in
addition to their displayed UTC-05:00 offset. Do not mix these clocks when
comparing events.

| UTC | Observation |
| --- | --- |
| Oct 23, 17:59:43 | SABnzbd service entered its running state; no restarts were recorded before the incident. |
| Oct 31, 17:19:44 | Last observed completed download before the failures. |
| Oct 31, 17:21:56 | First observed SABnzbd `Host is unreachable` warnings in the inspected 24-hour journal window. |
| Oct 31, 22:00:14 | Radarr's attempt to queue *It's the Great Pumpkin, Charlie Brown* failed because SABnzbd was unreachable. |
| Nov 1, approximately 02:24–02:28 | Live host inspection confirmed SABnzbd's endpoint and ARP response failure. |
| Nov 1, 02:28:52 | Operator restarted SABnzbd; the service and VM were recreated. |
| Nov 1, 02:28:53–02:28:54 | Newshosting connections negotiated TLS successfully. |
| Nov 1, 02:29:58 | A previously stuck download completed. |
| Nov 1, 02:48:32 | Downloads were still completing; the final HTTP probe returned 200. |

The last Radarr API check still reported `DownloadClientStatusCheck` with
`All download clients are unavailable due to failures`. Its two pending
movies (*Great Pumpkin* and *Halloweentown*) had
`status=downloadClientUnavailable`. Retry backoff was a possibility, not a
verified explanation. Full Radarr queue recovery was not established; do
not carry forward an assumption that the movie requests completed.

## Production evidence

The first pass used the Grafana MCP server's VictoriaLogs datasource,
`PD775F2863313E6C7`, with the discovered service labels `radarr` and
`sabnzbd`. Sonarr also reported that it could not reach SABnzbd.

The operator then explicitly authorized read-only live debugging through a
temporary SSH control socket named `nas.sock`. That was a session-specific
exception to this repository's production execution boundary. Do not assume
that the socket exists or that its authorization persists into a later
session; follow the current execution boundary and operator instructions.

Live observations before the restart:

- Radarr and Sonarr reported `Host is unreachable (sabnzbd.krun:8080)` while
  SABnzbd repeatedly reported `[Errno 113] Host is unreachable` connecting
  to `news.newshosting.com:563` / `news.iad.newshosting.com`.
- A host HTTP probe to `10.253.17.2:8080` timed out. A comparable probe to
  Radarr at `10.253.15.2:7878` returned HTTP 302.
- TAP `krun-51440` remained `UP,LOWER_UP`; networkd reported it configured
  and routable, with guest lease `10.253.17.2` offered to MAC
  `5a:94:ef:e4:0c:ee`. Host TAP MAC was `ce:40:80:84:93:c9`.
- The expected source policy routes and firewall rules were present.
  `nas-krun-network-policy`, `nas-egress-mullvad`, networkd, and nftables
  services were active. WireGuard had a recent handshake. These observations
  make a simple VPN outage a poor explanation for the local ARP failure;
  they do not prove every routing or firewall behavior was correct.
- A 15-second AF_PACKET observation, bound to the SABnzbd and Radarr TAPs,
  captured header metadata only. SABnzbd's TAP showed four host-origin TCP
  SYNs, three host ARP requests, no guest responses, and one guest-origin
  UDP multicast packet to `239.255.255.250`.
- Across several observations, host TAP TX bytes/packets stayed exactly
  `178297077328 / 74218429`, while TX drops rose from `542672` to `542711`.
  RX advanced slightly. Host TX means traffic toward the guest; host RX
  means traffic from the guest. Drops also increased during healthy traffic
  after restart, so an increasing drop count alone is not a failure signal.
- The application kept logging connection retries. The VM process existed;
  its `virtio-net worker` thread waited in `ep_poll`, and vCPUs waited in
  `kvm_vcpu_block`. A sleeping thread alone is normal and not proof of a
  deadlock. Combined with the traffic observations, it suggests a stalled
  receive path rather than an application process crash.
- Memory usage was about 1.37 GB, with a recorded peak about 2.20 GB. No
  host OOM evidence or host kernel warning was found in the bounded checks.
  Guest kernel logs and negotiated virtio features were not captured.

The restart restored both TAP traffic directions, HTTP service, and external
downloads without changing routing, firewall policy, or the VPN interface.
That narrows the fault to state reset by replacing the SABnzbd VM, but does
not isolate the faulty component within that VM/runtime boundary.

## Versions and upstream status at investigation time

Observed on the NAS:

| Component | Version |
| --- | --- |
| libkrun RPM | `1.19.6-1.fc44.x86_64` |
| libkrunfw RPM | `5.6.2-1.fc44.x86_64` |
| Host kernel | `7.2.7-200.fc44.x86_64` |
| crun RPM metadata | `1.28-1.fc44.x86_64` |

The repository replaces the packaged crun binary with a patched build, so
the RPM metadata does not establish the executed crun binary version.

Upstream's latest release was
[`v1.19.7`](https://github.com/libkrun/libkrun/releases/tag/v1.19.7), released
2026-10-22. The
[release comparison](https://github.com/libkrun/libkrun/compare/v1.19.6...v1.19.7)
showed no change to the receive worker or virtqueue implementation. Its
virtio-net change retries Unix datagram sends on EAGAIN, a different path
from this deployment's Linux TAP backend. Updating to 1.19.7 was therefore
not an established fix for this hypothesis.

Current `main` was also inspected and retained the relevant notification
ordering in `src/devices/src/virtio/net/worker/unix.rs`. Fedora's
[package page](https://packages.fedoraproject.org/pkgs/libkrun/libkrun/)
listed 1.19.6 as Fedora 44 stable. These are dated findings, not permanent
claims about what a future update contains.

Related upstream work was not a confirmed fix:

- [Issue #809](https://github.com/libkrun/libkrun/issues/809) concerns TSI
  with a busy CPU; this deployment uses TAP and did not show that symptom.
- [Issue #555](https://github.com/libkrun/libkrun/issues/555) concerns macOS
  Unix datagram receive errors under offload; it is not direct evidence for
  a Linux TAP failure.
- [PR #530](https://github.com/libkrun/libkrun/pull/530), a broader virtio-net
  I/O rewrite, was open and unmerged. It was not validated as a remedy here.

## Candidate mechanism: receive notification starvation

Relevant v1.19.6 sources:

- [`worker.rs`](https://github.com/libkrun/libkrun/blob/v1.19.6/src/devices/src/virtio/net/worker.rs)
- [`queue.rs`](https://github.com/libkrun/libkrun/blob/v1.19.6/src/devices/src/virtio/queue.rs)
- [`device.rs`](https://github.com/libkrun/libkrun/blob/v1.19.6/src/devices/src/virtio/net/device.rs)
- [`tap.rs`](https://github.com/libkrun/libkrun/blob/v1.19.6/src/devices/src/virtio/net/tap.rs)

`process_backend_socket_readable()` enables guest-to-device RX queue
notifications before `process_rx()`, then disables them afterward. Receiving
packets advances the queue's `next_avail` consumer position. With
`VIRTIO_RING_F_EVENT_IDX`, `enable_notification()` writes that position to
`avail_event`, and disabling notifications is a no-op: an event position
already crossed by the guest effectively suppresses further kicks until
rearmed. The net device advertises EVENT_IDX, although negotiation in the
failed guest was not established.

The hypothesis is that a burst drains the available buffers while leaving
`avail_event` behind the new consumer position. The guest refills buffers
but may correctly decide no kick is due. Meanwhile `process_rx()` can have
a deferred frame, and the TAP is monitored with edge-triggered epoll. If
receive work is left pending and no useful event causes another drain, the
worker can sleep while the guest remains alive and unable to receive.

Separately, `process_rx_queue_event()` ignores the boolean returned by
`enable_notification()`. The queue implementation documents that this
boolean detects work posted while notifications were being rearmed and
shows a drain/rearm/recheck loop. Ignoring it warrants investigation.

These code observations support the hypothesis. They do not establish the
actual failed ring indices, whether a TAP event would have rescued progress,
or whether another guest/runtime condition caused the same external
symptoms. The proposed ordering correction is not a tested production fix.

## Local experiment and its limits

A small Rust program imported the unmodified upstream v1.19.6 `queue.rs`,
created guest memory and two real descriptors, followed the receive
handler's notification ordering, consumed those descriptors, and modeled
the guest's refill notification decision using the wrapping
`vring_need_event` arithmetic.

The program produced:

```text
rearm_after_drain=false avail_event=0 guest_refill=2->4 guest_kicks=false
rearm_after_drain=true avail_event=2 guest_refill=2->4 guest_kicks=true
```

This demonstrates a notification-suppression scenario using the real queue
implementation. It does **not** execute the actual receive worker, event
loop, TAP backend, guest Linux network driver, or concurrent refill timing.
It is not an end-to-end reproduction or a regression test sufficient to
justify a patch on its own. Fedora's built library was not compared with
upstream source beyond its package version. The original failed VM was
gone before its ring/event state could be examined.

## If this is revisited

1. Recheck current upstream releases, Fedora patches, and relevant issues
   before doing implementation work. Learn the queue notification and TAP
   event behavior well enough to explain why a proposed change is correct.
2. Establish a controlled reproduction using the actual worker, real
   eventfds, and guest refill timing. Test pending-work races and deferred
   frame interrupt delivery; distinguish suppressed kicks from a durable
   receive stall. Seek evidence that could disprove this hypothesis.
3. Only then assess a narrow RX drain/rearm/recheck fix. Do not treat a
   single ordering swap as complete without testing concurrent refills,
   backpressure, EVENT_IDX behavior, and continued guest progress.
4. Validate in a disposable TAP VM under sustained receive load and
   periodic HTTP probes. Preserve the production VM boundary and egress
   policy. Do not stress-test or patch the production fleet to prove this.
5. Any upstream report or PR is a separate operator decision. Include the
   distinction between a reproduced code behavior and the unproven link to
   this NAS outage; require human understanding and review before sending.

If it recurs, the useful additional evidence is TAP counters over time,
bounded packet-header observations, the network worker's epoll registrations
and wait state, guest kernel/interface state if a supported diagnostic path
exists, and RX ring indices/`avail_event`/deferred-frame state if they can be
captured without perturbing production. Do not casually read arbitrary
guest memory or enable verbose payload logging. Decide how much evidence
to preserve before replacing the VM; a restart discards the stalled state.

For ordinary recovery, the operator-run SABnzbd restart worked in this
incident. Recovery verification should include HTTP, actual download
progress, and Radarr/Sonarr queue status separately. Their cached failure
state can outlast SABnzbd's recovery, and this session did not establish
how or when Radarr cleared its state.

## Reconstructing the small local experiment

Use a disposable local directory, not the NAS. Create `src/`, put the
manifest below in `Cargo.toml`, and the Rust listing below in `src/main.rs`.
Download the versioned upstream queue source as `queue.rs` beside the
manifest:

```bash
curl --fail --location \
  https://raw.githubusercontent.com/libkrun/libkrun/v1.19.6/src/devices/src/virtio/queue.rs \
  --output queue.rs
cargo run --quiet
```

The direct dependency versions below are those resolved in the original
experiment. This listing preserves the investigation without relying on
the session's temporary files; it is not part of the repository test suite.

```toml
[package]
name = "nas-rx-queue-repro"
version = "0.1.0"
edition = "2021"

[dependencies]
log = "=0.4.29"
virtio-bindings = "=0.2.7"
vm-memory = { version = "=0.17.1", features = ["backend-mmap"] }
```

```rust
#[macro_use]
extern crate log;
#[allow(dead_code)]
#[path = "../queue.rs"]
mod queue;
use queue::Queue;
use vm_memory::{Bytes, GuestAddress, GuestMemoryMmap};

fn need_event(event: u16, new: u16, old: u16) -> bool {
    new.wrapping_sub(event).wrapping_sub(1) < new.wrapping_sub(old)
}

fn scenario(rearm_after_drain: bool) -> bool {
    let mem = GuestMemoryMmap::<()>::from_ranges(&[(GuestAddress(0), 65536)]).unwrap();
    let mut q = Queue::new(2);
    q.size = 2;
    q.ready = true;
    q.desc_table = GuestAddress(0x1000);
    q.avail_ring = GuestAddress(0x2000);
    q.used_ring = GuestAddress(0x3000);
    q.set_event_idx(true);
    for i in 0..2u64 {
        mem.write_obj(0x4000u64 + i * 2048, GuestAddress(0x1000 + i * 16)).unwrap();
        mem.write_obj(1536u32, GuestAddress(0x1008 + i * 16)).unwrap();
        mem.write_obj(2u16, GuestAddress(0x100c + i * 16)).unwrap();
        mem.write_obj(i as u16, GuestAddress(0x2004 + i * 2)).unwrap();
    }
    mem.write_obj(2u16, GuestAddress(0x2002)).unwrap();
    // Same ordering as 1.19.6 process_backend_socket_readable().
    q.enable_notification(&mem).unwrap();
    while let Some(desc) = q.pop(&mem) {
        let index = desc.index;
        q.add_used(&mem, index, 100).unwrap();
    }
    q.disable_notification(&mem).unwrap();
    if rearm_after_drain {
        assert!(!q.enable_notification(&mem).unwrap());
    }
    // Linux guest replenishes two consumed buffers and checks vring_need_event.
    let avail_event: u16 = mem.read_obj(GuestAddress(0x3000 + 4 + 8 * 2)).unwrap();
    mem.write_obj(4u16, GuestAddress(0x2002)).unwrap();
    let kick = need_event(avail_event, 4, 2);
    println!("rearm_after_drain={rearm_after_drain} avail_event={avail_event} guest_refill=2->4 guest_kicks={kick}");
    kick
}

fn main() {
    assert!(!scenario(false), "upstream ordering suppresses the refill notification");
    assert!(scenario(true), "rearming after draining restores the refill notification");
}
```
