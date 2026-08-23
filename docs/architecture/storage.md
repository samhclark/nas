# Service storage architecture

`quadlets/<service>.toml` is the only authored description of application
storage. A storage declaration simultaneously defines the host lifecycle, the
container mount, the boot-time preparation unit, and the rootless service's
current-boot readiness check. Raw `[[container.volumes]]` sources below `/var`
are rejected.

This follows the useful part of Android's application model: each service has
a stable identity, explicitly declares the state it can see, and receives only
the derived mounts and host preparation needed for that declaration. The
generator acts as a small policy compiler rather than relying on each service
to reproduce filesystem setup correctly.

## Storage kinds

`kind = "directory"` creates a service-owned directory below `/var`. Optional
subdirectories are explicit. These small root-filesystem trees use guarded
automatic ownership and SELinux repair.

`kind = "managed-zfs"` creates a dataset below `tank/<service>/` only when it
is absent. Record size, compression, atime, primary-cache policy, mountpoint,
owner, mode, and persistent `container_file_t:s0` labeling are explicit. An
existing dataset is verified; properties are never changed in place.

`kind = "existing-zfs"` requires the shared `tank/videos` dataset. It is
read-only by construction. The storage runtime may repair its SELinux labels
after an explicit request, but it never creates the dataset or changes its
ownership or modes.

The authored media-automation layout is a TRaSH-style single `/data` tree:

```text
/var/zfs/tank/videos/data/
├── media/
│   ├── movies/
│   └── tv/
└── usenet/
    ├── incomplete/
    └── complete/
        ├── movies/
        └── tv/
```

The fleet resource requires these directories to exist as real directories
under the `tank/videos` mount, owned by `root:52000`, mode `2775`, and labeled
`container_file_t:s0`. The `media` group is a stable fleet group with GID
`52000`. Sonarr, Radarr, SABnzbd, and Jellyfin receive that group; the first
three receive writable access to the appropriate part of `/data`, while
Jellyfin receives `/data/media` read-only.
Prowlarr receives no media mount. The resource preparation unit only verifies
this existing shared tree; it does not create, move, chown, or relabel it.

The four application configuration datasets are separate managed ZFS state:

| Service | Dataset and host mount | Container path | ZFS policy |
| --- | --- | --- | --- |
| Sonarr | `tank/sonarr/config` → `/var/lib/sonarr/config` | `/config` | `recordsize=16K`, `compression=lz4`, `atime=off`, `primarycache=all` |
| Radarr | `tank/radarr/config` → `/var/lib/radarr/config` | `/config` | same |
| Prowlarr | `tank/prowlarr/config` → `/var/lib/prowlarr/config` | `/config` | same |
| SABnzbd | `tank/sabnzbd/config` → `/var/lib/sabnzbd/config` | `/config` | same |
| Jellyfin | existing `tank/jellyfin/{config,cache}` | `/config`, `/cache` | existing Jellyfin declaration |

The media files themselves are intentionally not backed up. The current
`tank/videos` snapshot policy has frequent, hourly, daily, and weekly timers
with retentions of 5, 25, 8, and 5 snapshots respectively. There are no
monthly or yearly `videos` timers; the five weekly snapshots are the outer
retention boundary of approximately one month. These snapshots are short-lived
recovery aids, not a media backup strategy.

Every `[[storage.exports]]` names a relative source, an absolute container
path, and `read-only` or `read-write` access. Generated Quadlets do not use
Podman `:z` or `:Z` for these trees.

## Generated enforcement

The compiler derives four artifacts from the typed declaration:

- `Volume=` entries in the rootless Quadlet;
- `/usr/share/nas/storage/<service>.storage-manifest`;
- `nas-prepare-<service>-storage.service`;
- a bounded `ExecStartPre` check of `/run/nas-storage/<service>/ready`, exact
  ZFS mount sources, service ownership, and service-user access.

`fleet/storage-units.list` is the Containerfile enablement source. Adding
storage never requires a second handwritten unit list.

The runtime parses its manifest completely before inspecting or changing the
host. Directory-only services do not require ZFS. ZFS use is restricted to
`zpool list` and `zfs list`, `get`, and `create`; pool creation/import/export,
dataset destruction, rollback, property mutation, and snapshot retention are
outside this engine. Snapshot expiry remains a separate, narrowly reviewed
unit because it intentionally destroys only expired snapshots.

Owned storage roots require the service's exact host UID/GID, declared mode,
and canonical `system_u:object_r:container_file_t:s0` context. The bounded
descendant sample has a different contract because rootless user namespaces
can legitimately create files as either the service host UID/GID or an ID in
that service's generated 65,536-ID subordinate range. A GID declared through
the service's typed supplemental fleet groups is also valid; this matters when
an application's primary in-container GID maps to the shared `media` group.
The versioned storage manifest carries those GIDs into both bounded readiness
and explicit-repair verification so an app-created config file remains valid
after reboot. Its SELinux user field may also differ; readiness enforces the
security-relevant `object_r:container_file_t:s0` suffix and rejects MCS
categories. IDs outside
the service identity, declared supplemental GIDs, and assigned subordinate
range, wrong SELinux types, and non-`s0` ranges remain drift.

## Repair policy

Normal boot performs root and one-descendant checks, not unbounded scans.
Small directory state is repaired automatically only after the service's
rootless container is confirmed stopped and its declared TCP ports are free.

Established ZFS trees require an explicit durable request:

```text
/var/lib/nas-repairs/<service>/repair-required
```

New empty managed datasets initialize automatically. A private dataset root
left owned by `root:root` records an interrupted repair and resumes
automatically. The runtime arms every private root in the service group before
recursive work, removes the explicit request only after the whole group
passes, and publishes readiness last. Recursive traversals stay on one
filesystem, prune `.zfs`, and use `restorecon -F -R -x`.

An explicit-repair-required refusal exits with status 78. Generated storage
units declare that status in `RestartPreventExitStatus`, so deterministic
operator intervention does not become a 30-second retry loop; transient
failures retain the existing restart policy. Drift logs name the exact sampled
path and observed ownership, mode, or label before refusing repair.

The current authored stateful services are Caddy, Alertmanager, Grafana,
Garage, VictoriaMetrics, VictoriaLogs, Jellyfin, all four Immich components,
and the four media-automation services. VictoriaLogs storage is deployed and
validated through the initial logging pilot. The media-automation config
datasets and shared layout are deployed and validated with the four production
services. `tank/victoria-logs/data` deliberately has no snapshot or backup
policy.
Blackbox exporter, vmalert, and Jellyfin exporter are intentionally stateless;
image assets and runtime secrets are separate declaration classes.
