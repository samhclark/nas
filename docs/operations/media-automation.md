# Media automation operations

## Status and scope

This document describes the authored first Arr-stack slice:

- Sonarr for TV;
- Radarr for movies;
- Prowlarr for indexers;
- SABnzbd for Usenet downloads; and
- the existing Jellyfin media server.

The four services are rootless LSIO containers adapted for this host's libkrun
execution boundary. Each has a dedicated TAP guest and a separate managed
config dataset. Sonarr, Radarr, Prowlarr, and SABnzbd are deployed and
operational on the NAS, and all are selected for Mullvad egress. This runbook
describes their current topology and configuration; it is not a substitute for
checking live service health, a successful WireGuard handshake, or a completed
download when performing an operational change.

## Runtime shape

The containers use the generated `*.krun` names and are not connected by host
port publishing. Their initial declared endpoints and allowed consumers are:

| Service | Endpoint | Consumers |
| --- | --- | --- |
| Sonarr | `sonarr.krun:8989` | Caddy, Prowlarr |
| Radarr | `radarr.krun:7878` | Caddy, Prowlarr |
| Prowlarr | `prowlarr.krun:9696` | Caddy, Sonarr, Radarr |
| SABnzbd | `sabnzbd.krun:8080` | Caddy, Sonarr, Radarr |

The management UIs are reached through Caddy at:

```text
https://sonarr.i.samhclark.com
https://radarr.i.samhclark.com
https://prowlarr.i.samhclark.com
https://sabnzbd.i.samhclark.com
```

Caddy's shared private-ingress handler allows LAN private ranges and the
Tailscale `100.64.0.0/10` range, and returns `403` for other source ranges.
These four UIs therefore have no intended WAN access. The Caddy hostnames are
the right values for a human browser; service-to-service configuration should
use the `*.krun` endpoints below so it stays on the declared inter-TAP edges.

## DNS and egress boundary

`wg-arr` is a root-managed host WireGuard interface generated from the typed
`[egress.mullvad]` declaration in `quadlets/_fleet.toml`. That declaration is
the sole authored source for the Mullvad multihop endpoint. The private key
comes from the encrypted SOPS key `mullvad-private-key` and is loaded into
systemd-networkd with a systemd credential. Generated networkd files are
runtime artifacts, not a second configuration source.

The selected services use `wg-arr` for non-DNS Internet traffic. Their only
DNS server is Tailscale MagicDNS at `100.100.100.100`. The Tailnet is
configured to override device DNS defaults and use Quad9's public resolver
boundary, with Quad9 DoH where enabled by the Tailnet configuration. The
containers do not use Comcast resolvers or Mullvad's `10.64.0.1` resolver.

The generated host policy allows selected guests to send DNS to
`100.100.100.100` only through `tailscale0`; all other traffic must leave
through `wg-arr`, and guest-specific established-traffic and final drops block
fallback even if an existing connection is rerouted after WireGuard disappears. The
`nas-egress-mullvad.service` readiness marker proves only that the interface,
routes, chains, and selected rules are present for the current boot. A
successful handshake and leak test still require operator validation after
deployment.

## Exact initial application configuration

Configure these paths and endpoints after the services are deployed and their
UIs are reachable. The paths are container paths, not host paths.

| Component | Initial value |
| --- | --- |
| SABnzbd incomplete folder | `/data/usenet/incomplete` |
| SABnzbd completed TV folder | `/data/usenet/complete/tv` |
| SABnzbd completed movie folder | `/data/usenet/complete/movies` |
| Sonarr root folder | `/data/media/tv` |
| Radarr root folder | `/data/media/movies` |
| Sonarr download client | SABnzbd at `http://sabnzbd.krun:8080` |
| Radarr download client | SABnzbd at `http://sabnzbd.krun:8080` |
| Sonarr indexer manager | Prowlarr at `http://prowlarr.krun:9696` |
| Radarr indexer manager | Prowlarr at `http://prowlarr.krun:9696` |
| Jellyfin TV library | `/data/media/tv` |
| Jellyfin movie library | `/data/media/movies` |

Use the APIs or built-in sync functions to connect Prowlarr to Sonarr and
Radarr. Prowlarr's indexer definitions and the application API keys are
configuration data stored in the individual config datasets; they are not
repo-managed secrets. Do not configure the apps with host paths such as
`/var/zfs/tank/videos/...`, because those paths do not exist inside the
containers and would defeat the shared `/data` contract.

## Storage and backup boundary

The shared existing ZFS dataset is `tank/videos`, mounted at
`/var/zfs/tank/videos`. Its required layout is:

```text
/var/zfs/tank/videos/data/media/movies
/var/zfs/tank/videos/data/media/tv
/var/zfs/tank/videos/data/usenet/incomplete
/var/zfs/tank/videos/data/usenet/complete/movies
/var/zfs/tank/videos/data/usenet/complete/tv
```

The layout is intentionally a single `/data` tree so imports can use atomic
renames and hardlinks where an application supports them. The shared group is
GID `52000` (`media`). Sonarr, Radarr, SABnzbd, and Jellyfin are members of
that group; shared directories use group `52000`, mode `2775`, and inherited
setgid semantics. Shared regular files are group-readable and group-writable.
Jellyfin mounts only `/data/media`, read-only. The media payload is
intentionally not backed up because of its size. `tank/videos` does retain
short-lived frequent, hourly, daily, and weekly snapshots; the five weekly
snapshots are the approximately one-month outer boundary. No monthly or
yearly snapshot timer is configured for this dataset.

Each Arr service has its own managed config dataset:
`tank/sonarr/config`, `tank/radarr/config`, `tank/prowlarr/config`, and
`tank/sabnzbd/config`. These are application state and should be treated
separately from the intentionally unbacked media payload.

## Operator migration runbook

The following is a reviewed runbook for an operator on the NAS. No command in
this document has been run by the coding agent. Run one phase at a time and
return the requested output before proceeding. The commands use explicit
paths, refuse to overwrite existing destination directories, and do not use
destructive recursive deletion.

### Phase 0: read-only preflight

This phase changes nothing. The reviewed collector is
[`scripts/diagnostics/media-automation-preflight.sh`](../../scripts/diagnostics/media-automation-preflight.sh).
Copy that file to the NAS, then run this single command from the directory
containing it:

```bash
sudo bash ./media-automation-preflight.sh | tee media-automation-preflight.txt
```

Return `media-automation-preflight.txt`. The script records the current mount,
dataset, directory, ownership, mode, labels, file types, symlinks, and available
space. It is kept outside the image because this preflight happens before the
new image is deployed.

Stop here if `tank/videos` is not the mounted source, `findmnt -R` or `zfs
list -r` shows a nested mount/dataset, either old source path is missing or is
a symlink, any destination already exists with content, or the space/type
output is not understood. Return the output and resolve the discrepancy before
changing anything. Symlinks inside the source trees are reported for review;
later phases preserve but never follow them.

### Phase 1: stop Jellyfin and confirm the stop

The existing Jellyfin mount is read-only, but it must still be stopped before
moving its library directories so it cannot scan while paths change. This is a
state-changing phase; run it only after reviewing Phase 0. Copy
[`scripts/operations/media-automation-stop-jellyfin.sh`](../../scripts/operations/media-automation-stop-jellyfin.sh)
to the NAS, then run this single command from the directory containing it:

```bash
bash -o pipefail -c 'sudo bash ./media-automation-stop-jellyfin.sh 2>&1 | tee media-automation-stop-jellyfin.txt'
```

Return `media-automation-stop-jellyfin.txt`. The script validates the Jellyfin
account and home, records the initial unit states, stops the user service before
stopping its manager, and fails unless the final manager state is inactive and
no process remains under Jellyfin's dedicated UID. The outer shell's `pipefail`
preserves a script failure even though the output is also written through
`tee`. Stopping the user manager is included as a belt-and-suspenders guard.

### Phase 2: create the target layout

This phase creates only the required empty directory tree and sets the shared
root contract. It does not move, relabel, or modify media files, and it does
not modify the existing `movies` or `tv-shows` trees. It rejects symlinks,
non-directories, unexpected entries under the target subtree, populated
destinations, a wrong mount source, and a different device. An idempotent rerun
is safe only while every destination remains empty; existing empty directories
may have their declared ownership and mode restored. Copy
[`scripts/operations/media-automation-create-layout.sh`](../../scripts/operations/media-automation-create-layout.sh)
to the NAS, then run this single command from the directory containing it:

```bash
bash -o pipefail -c 'sudo bash ./media-automation-create-layout.sh 2>&1 | tee media-automation-create-layout.txt'
```

Return `media-automation-create-layout.txt`. The script emits concise mount,
device, exact ownership/mode, and source-preservation evidence. Do not
continue if it reports a populated destination or any validation failure.

### Phase 3: move the existing library directories without overwrite

This is the only phase that moves existing media. The operation is a
fail-closed, same-dataset rename with no recursive copy or deletion. It
revalidates the exact mount, Phase-2 root metadata, source and destination
types/devices, supported source entry types, Jellyfin shutdown, and open
`/proc` process references. It normalizes shared-group access before renaming,
preserves existing user owners except for the two source roots, verifies the
recursive contract, captures source identities, and uses an explicit rollback
if the rename stage or its checks fail. `mv --no-copy` forbids cross-filesystem
copy-and-delete fallback. Rollback status is printed clearly;
manual intervention is required after a rollback failure. It performs no
relabeling, deployment, media deletion, or Jellyfin restart.

The `/proc` checks are conservative point-in-time checks, not a kernel write
lock. Do not start another media copy, shell, scanner, or service while this
phase runs. This is a single-operator maintenance window; an uncooperative
root process could always race path-based administration.

Copy [`scripts/operations/media-automation-migrate-library.sh`](../../scripts/operations/media-automation-migrate-library.sh)
to the NAS, then run this single command from the directory containing it:

```bash
bash -o pipefail -c 'sudo bash ./media-automation-migrate-library.sh 2>&1 | tee media-automation-migrate-library.txt'
```

Return `media-automation-migrate-library.txt`. Do not continue to Phase 4
unless it reports migration success, absent old paths, matching captured
device/inode identities, exact required-root metadata, and recursive shared
group verification.

### Phase 4: apply the persistent SELinux label and verify the layout

This phase changes only SELinux extended attributes. It first proves that the
host is enforcing SELinux and that the deployed policy recursively maps every
path in the relabel scope to `object_r:container_file_t:s0`, then runs one
bounded `restorecon -F -R -x`
over the media dataset while excluding the visible ZFS snapshot control
directory. It verifies actual labels over that same full-dataset scope,
destination device/inode identities and counts, required-root ownership/modes, the
recursive shared-group contract, and Jellyfin shutdown. It never moves or
deletes media, crosses into another filesystem, traverses snapshots, deploys,
or restarts Jellyfin.

Copy [`scripts/operations/media-automation-relabel.sh`](../../scripts/operations/media-automation-relabel.sh)
to the NAS, then run this single command from the directory containing it:

```bash
bash -o pipefail -c 'sudo bash ./media-automation-relabel.sh 2>&1 | tee media-automation-relabel.txt'
```

Return `media-automation-relabel.txt`. Do not continue to Phase 5 unless it
reports successful policy, relabel, identity/count, ownership/mode, label, and
Jellyfin checks.

### Phase 5: deploy and validate in bounded increments

After the migration output is reviewed, publish/deploy the image through the
normal repository workflow. Then validate in this order, returning output for
each step:

1. Confirm `sops-distribute-secrets.service`, `systemd-networkd`,
   `nftables`, `tailscaled`, `nas-egress-mullvad.service`, and the four
   `nas-prepare-*-storage.service` units are healthy.
2. Confirm `wg-arr` has `10.72.38.9/32`, the `51820` default route, and a
   recent WireGuard handshake. The readiness marker alone is insufficient.
3. From each selected guest, verify MagicDNS resolution and a public egress
   address; verify DNS never uses a Comcast resolver and that the public
   address is Mullvad. Also test that withdrawing `wg-arr` causes public
   traffic to fail rather than escape through WAN.
4. Open each Caddy hostname from both an allowed LAN/Tailscale client and a
   disallowed WAN path. The former should reach its UI; the latter should get
   `403` or be unreachable.
5. Configure the exact application paths and `*.krun` endpoints in the table
   above, then test one small SABnzbd download through Sonarr or Radarr and
   confirm the completed file lands in the correct media directory and is
   visible to Jellyfin.

Return unit status, interface/routes/handshake evidence, DNS and egress test
results, ingress results, and the final file paths. Do not call the deployment
validated until all five groups have evidence.
