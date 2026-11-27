# Media automation operations

## Status and scope

Sonarr, Radarr, Prowlarr, and SABnzbd are deployed and operational on the
NAS alongside Jellyfin. Bazarr adds subtitle automation to the same fleet.
Its first-use and rollout checks are below.

Each automation service uses a rootless LinuxServer image adapted for
libkrun, a dedicated TAP guest, a managed config dataset, and Mullvad egress.

## Runtime shape

The containers use the generated `*.krun` names and are not connected by host
port publishing. The declared endpoints and allowed consumers are:

| Service | Endpoint | Consumers |
| --- | --- | --- |
| Sonarr | `sonarr.krun:8989` | Caddy, Prowlarr, Bazarr |
| Radarr | `radarr.krun:7878` | Caddy, Prowlarr, Bazarr |
| Prowlarr | `prowlarr.krun:9696` | Caddy, Sonarr, Radarr |
| SABnzbd | `sabnzbd.krun:8080` | Caddy, Sonarr, Radarr, Prowlarr |
| Bazarr | `bazarr.krun:6767` | Caddy |

The management UIs are reached through Caddy at:

```text
https://sonarr.i.samhclark.com
https://radarr.i.samhclark.com
https://prowlarr.i.samhclark.com
https://sabnzbd.i.samhclark.com
https://bazarr.i.samhclark.com
```

Caddy's shared private-ingress handler allows LAN private ranges and the
Tailscale `100.64.0.0/10` range, and returns `403` for other source ranges.
These UIs therefore have no intended WAN access. The Caddy hostnames are
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

## Bazarr first use and rollout checks

1. After the usual NAS image update, check the Bazarr user unit, its config
   readiness unit, and the shared media readiness unit. Confirm the UI works
   over Caddy from LAN or Tailscale and receives HTTP 403 or is unreachable from
   outside those networks. Enable Bazarr UI authentication under Settings →
   General → Security.
2. In Settings → Sonarr and Settings → Radarr, enable each integration and
   enter `sonarr.krun` on port `8989` and `radarr.krun` on port `7878`, using
   HTTP and each service's API key from its own UI. Use the connection test for
   each. Store the keys and provider credentials in Bazarr's private config.
   Leave Path Mappings empty for both integrations. Bazarr sees the same
   `/data/media/tv` and `/data/media/movies` paths that the APIs report.
3. Set Subtitle Folder to **Alongside Media File** and leave Bazarr's
   permission override disabled so the entrypoint's `umask 002` applies.
   Choose subtitle providers and language profiles, then assign profiles to a
   small sample of existing movies and series first. See the
   [TRaSH Bazarr setup guide](https://trash-guides.info/Bazarr/Setup-Guide/) and
   [existing-library configuration](https://trash-guides.info/Bazarr/After-install-configuration/).
4. Download a sidecar for one existing movie and one existing episode.
   Confirm both land alongside the videos, have host group `52000` and
   group read/write access, and appear in Jellyfin after scanning. If writes
   fail for an individual title, inspect that directory's group, setgid,
   access bits, and SELinux label first. The readiness gate checks the mount
   and declared roots. Older title directories may have drifted independently.
   Verify provider traffic uses Mullvad and fails closed if `wg-arr` is
   withdrawn during an operator maintenance window.
5. Once the sample passes, open Settings → Languages → Profiles in Bazarr.
   Under **Default Language Profiles For Newly Added Shows**, enable **Series**
   and **Movies**, select the desired profile for each, and save. These defaults
   apply when Bazarr first imports a title. In the Series and Movies views,
   select the existing titles you want covered, use **Change Profile** in the
   bulk toolbar, and save each selection.
6. On the next new Sonarr series and Radarr movie, wait for Bazarr to import
   each title. Check its profile in Bazarr before assigning anything manually.
   Both titles should already have their respective default profiles.

Bazarr's guest UID/GID `1000:1000` map to host UID `51450` and media GID
`52000`. Its writable media mount is limited to `/data/media`. The existing
shared storage policy supplies the SELinux label, and setgid library directories
preserve the shared group for new sidecars.

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
GID `52000` (`media`). Sonarr, Radarr, SABnzbd, Bazarr, and Jellyfin are members of
that group; shared directories use group `52000`, mode `2775`, and inherited
setgid semantics. Shared regular files are group-readable and group-writable.
Jellyfin mounts only `/data/media`, read-only. The media payload is
intentionally not backed up because of its size. `tank/videos` does retain
short-lived frequent, hourly, daily, and weekly snapshots; the five weekly
snapshots are the approximately one-month outer boundary. No monthly or
yearly snapshot timer is configured for this dataset.

Each automation service has its own managed config dataset.
`tank/sonarr/config`, `tank/radarr/config`, `tank/prowlarr/config`,
`tank/sabnzbd/config`, and `tank/bazarr/config`. These datasets store
application state separately from the media payload.

On a fresh host the operator prepares the required roots on the mounted
dataset with owner `root:media`, mode `2775`, and label
`container_file_t:s0`. The shared-storage unit verifies these roots on boot.
The original migration from `movies` and `tv-shows` is recorded at Git
revision `fc0eeb9` in this runbook and its companion scripts.

## Verify a deployment

1. Confirm `sops-distribute-secrets.service`, `systemd-networkd`,
   `nftables`, `tailscaled`, `nas-egress-mullvad.service`, and the relevant
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
   visible to Jellyfin. For a hardlink import, confirm the completed and imported
   files have the same device and inode.
