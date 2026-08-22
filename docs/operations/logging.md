# Host logging rollout

The deployed logging path is:

```text
persistent journald -> host Vector -> loopback:9428 -> VictoriaLogs/libkrun
                                                     -> Grafana Explore
```

The production-confirmed pilot forwards journald records selected by the Caddy
UID (`51310`) and VictoriaMetrics UID (`51250`). The operator confirmed that
this initial path works on the NAS. Additional collection groups retain their
own production-validation gates; Caddy access logs and Mullvad/WireGuard
logging remain separate projects.

VictoriaLogs stores seven days of searchable history on
`tank/victoria-logs/data`, with a pool-filesystem usage guard at 80 percent.
Grafana Explore reaches it through the provisioned, non-default VictoriaLogs
datasource. Logs therefore inherit Grafana's current anonymous-admin access
boundary; VictoriaLogs is not published through Caddy.

The local journal remains the emergency diagnostic source. The intended host
journald policy is persistent storage under `/var/log/journal`, compressed and
bounded to seven days or 512 MiB, with a 2 GiB free-space reserve. Vector's
checkpoint and disk buffer remain on the root filesystem under
`/var/lib/nas-vector`; its runtime files are ephemeral under `/run/nas-vector`.
The pre-deployment journal used about 3.9 GiB, so the first boot with this
policy is expected to vacuum older history down toward the new 512 MiB bound.

Delivery is at-least-once. During a prolonged VictoriaLogs outage, Vector first
uses its 1 GiB disk buffer and then backpressures journald. If journald itself
reaches its configured limits, the oldest records can be vacuumed. Restarts
may duplicate records and are acceptable for this pilot.

## Fleet rollout plan

The transport path is now considered high-confidence (approximately 90--95
percent) for additional services. The remaining work is service-specific
configuration and production evidence: enabling useful console output,
confirming the expected records, and exercising outage, restart, and reboot
recovery one application group at a time.

The first expansion group is configured in the image and awaits its staged
production validation:

- Garage
- vmalert
- Alertmanager
- blackbox-exporter
- Grafana
- Jellyfin exporter

The second expansion group is also configured, but must not be deployed until
Group 1 passes its production gate:

- Immich server
- Immich machine learning
- Immich PostgreSQL
- Immich Valkey

The third expansion group is configured behind both earlier production gates:

- Jellyfin runtime logs

The final planned runtime group is configured behind all three earlier gates:

- Sonarr
- Radarr
- Prowlarr
- SABnzbd

The existing Caddy and VictoriaMetrics sources are the production-confirmed
initial backfill and delivery test. Group 1 extends that same path. Keep the
new group's status as configured-but-unvalidated until it has completed the
read-only checks below and the sampled post-expansion outage, restart, and
reboot checks. Validate each group before enabling the next one, while
accepting the documented at-least-once duplicates and bounded eventual loss.

The typed Quadlet schema is the source of truth for collection membership.
Vector UID sources and their low-cardinality service tags are generated from
each service's `container.log-driver = "journald"` declaration alongside the
other fleet artifacts. This prevents
the collector's UID selectors from becoming a separately maintained list and
makes omissions or mismatched host identities fail during repository checks.

After Group 1, deploy and validate the configured Immich group, followed by the
configured Jellyfin runtime group. Keep Jellyfin's local file and transcode
diagnostics in place; journald collection supplements those files. Then deploy
the configured Sonarr, Radarr, Prowlarr, and SABnzbd media-automation group.
The repo-owned adapters provide structured Servarr console output and explicit
SABnzbd console output while retaining the applications' file logs, so image
vendoring is not a prerequisite for this transport path.

Caddy access logs remain a separate policy and volume decision from Caddy
runtime logs. Host Mullvad and WireGuard logs are a separate host-networking
project, not a container-image logging migration. No disclosure or redaction
gate is required for this single-owner personal NAS; the operator accepts the
contents of these logs as part of the machine's private data.

## Pre-expansion gate

Run these read-only checks on the NAS before deployment:

```bash
findmnt -no SOURCE,FSTYPE,TARGET -T /var
findmnt -no SOURCE,FSTYPE,TARGET -T /var/log/journal
df -h /var /run
sudo journalctl --disk-usage
sudo journalctl --list-boots --no-pager | head

for spec in \
  'garage 51110' \
  'vmalert 51220' \
  'blackbox-exporter 51230' \
  'alertmanager 51240' \
  'grafana 51210' \
  'jellyfin-exporter 51260'; do
  read -r service uid <<<"${spec}"
  printf '\n== %s UID %s (bounded metadata sample) ==\n' "${service}" "${uid}"
  sudo journalctl -b -n 200 --no-pager -o json "_UID=${uid}" |
    jq -r '[._UID, ._SYSTEMD_UNIT, ._SYSTEMD_USER_UNIT, ._TRANSPORT, .SYSLOG_IDENTIFIER, .CONTAINER_NAME] | @tsv' |
    sort -u | sed -n '1,20p'
done
```

Do not expand collection if `/var` has less than 5 GiB free or a Group 1
service lacks useful UID-selected metadata. The original pre-deployment
evidence passed both gates: `/var` had 436 GiB free, the persistent journal
spanned 155 historical boots, and both pilot UIDs exposed useful records.

## Validation sequence

Keep Group 1 in the configured-but-unvalidated state until the operator has
completed the read-only checks and the sampled recovery checks below:

1. Read-only checks: service health, mounted storage, journal limits, Vector
   state placement, VictoriaLogs metrics, Grafana queries, buffer size, and
   SELinux denials.
2. After Group 1 is enabled, stop VictoriaLogs once in a controlled window.
   Confirm Vector remains running, the SSD buffer grows, and the buffer drains
   after VictoriaLogs recovers.
3. Restart Vector once. Confirm checkpoint and buffer recovery; tolerate only
   the documented at-least-once duplicates.
4. Reboot cleanly once. Confirm prior-boot journal availability, late
   VictoriaLogs startup tolerance, and records spanning the reboot.
5. Measure root-journal growth, Vector buffer writes, VictoriaLogs dataset
   growth, and HDD behavior before expanding collection again. These are
   one-time sampled checks for the first expansion; subsequent groups still
   receive their own read-only validation gate.

Do not access the production NAS from development automation. Prepare reviewed
operator commands and use returned evidence for every live validation stage.

## Stage 1 read-only command

After the changed image boots, run this command before either controlled
restart test. It does not change service or storage state and does not query
application log payloads; the bounded Vector journal tail remains visible for
collector diagnostics:

```bash
for spec in \
  '_nas_caddy 51310 caddy.service' \
  '_nas_victoriametrics 51250 victoria-metrics.service' \
  '_nas_garage 51110 garage.service' \
  '_nas_vmalert 51220 vmalert.service' \
  '_nas_blackbox 51230 blackbox-exporter.service' \
  '_nas_alertmanager 51240 alertmanager.service' \
  '_nas_grafana 51210 grafana.service' \
  '_nas_immichserver 51130 immich-server.service' \
  '_nas_immichdatabase 51140 immich-database.service' \
  '_nas_immichvalkey 51150 immich-valkey.service' \
  '_nas_immichmachinelearning 51160 immich-machine-learning.service' \
  '_nas_jellyfin 51120 jellyfin.service' \
  '_nas_jellyfinmetrics 51260 jellyfin-exporter.service' \
  '_nas_sonarr 51410 sonarr.service' \
  '_nas_radarr 51420 radarr.service' \
  '_nas_prowlarr 51430 prowlarr.service' \
  '_nas_sabnzbd 51440 sabnzbd.service' \
  '_nas_victorialogs 51270 victoria-logs.service'; do
  read -r user uid unit <<<"${spec}"
  printf '\n== %s (%s) ==\n' "${unit}" "${uid}"
  sudo systemctl is-active "user@${uid}.service"
  sudo -u "${user}" env \
    HOME="/var/home/${user}" \
    XDG_RUNTIME_DIR="/run/user/${uid}" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
    systemctl --user show "${unit}" \
      -p LoadState -p ActiveState -p SubState -p NRestarts
done

printf '\n== Vector ==\n'
sudo systemctl is-active nas-vector.service
sudo systemctl show nas-vector.service \
  -p LoadState -p ActiveState -p SubState -p MainPID -p NRestarts
sudo journalctl -u nas-vector.service -b -n 50 --no-pager

printf '\n== Journal and storage ==\n'
test -d /var/log/journal && echo persistent-journal-directory=present
sudo systemd-analyze cat-config systemd/journald.conf |
  grep -E '^(Storage|Compress|SystemMaxUse|SystemKeepFree|SystemMaxFileSize|MaxRetentionSec|RuntimeMaxUse|RuntimeKeepFree|RuntimeMaxFileSize)='
sudo journalctl --disk-usage
sudo journalctl -b -1 -n 1 --no-pager
sudo findmnt -no SOURCE,FSTYPE,TARGET -T /var/lib/victoria-logs
sudo zfs list -H -o name,mountpoint,used,avail tank/victoria-logs/data
sudo zfs get -H -o property,value \
  recordsize,compression,atime,primarycache tank/victoria-logs/data
sudo stat -c '%U:%G %a %C %n' /var/lib/victoria-logs
sudo du -shL /var/lib/nas-vector
findmnt -no SOURCE,FSTYPE,TARGET -T /run/nas-vector
sudo find /run/nas-vector -mindepth 1 -maxdepth 2 \
  -printf '%M %u:%g %s %p\n' 2>/dev/null || true

printf '\n== VictoriaLogs and scrape health ==\n'
curl -fsS http://127.0.0.1:9428/metrics | sed -n '1,5p'
curl -fsS 'http://127.0.0.1:8428/api/v1/targets?state=active' |
  jq -r '.data.activeTargets[]
    | select(.labels.job=="victoria-logs")
    | [.labels.job,.health,(.lastError // ""),.lastScrape] | @tsv'
curl -fsS http://127.0.0.1:3000/api/health
curl -fsS http://127.0.0.1:3000/api/datasources/name/VictoriaLogs |
  jq -e '{name,type,url,isDefault,jsonData}'

printf '\n== VictoriaLogs count-only checks (last 24h) ==\n'
for service in \
  caddy victoria-metrics garage vmalert blackbox-exporter alertmanager grafana \
  immich-server immich-database immich-valkey immich-machine-learning \
  jellyfin jellyfin-exporter sonarr radarr prowlarr sabnzbd; do
  curl -fsSG http://127.0.0.1:9428/select/logsql/query \
    --data-urlencode "query=_stream:{host=\"nas\",service=\"${service}\"} | stats count()" \
    --data-urlencode 'start=now-24h' \
    --data-urlencode 'end=now' \
    --data-urlencode 'limit=1' |
    sed "s/^/${service}: /"
done

printf '\n== SELinux AVCs ==\n'
sudo ausearch -m AVC -ts boot -i 2>/dev/null |
  grep -Ei 'vector|victoria|grafana|caddy|garage|vmalert|blackbox|alertmanager|immich|jellyfin' ||
  echo 'No matching logging-rollout AVCs'
```

Then open Grafana Explore, select the non-default `VictoriaLogs` datasource,
and run `_stream:{host="nas",service="garage"}`. Keep Group 1
unvalidated if a service has no recent count, the scrape is unhealthy, the
datasource fails in Explore, or matching SELinux denials are present. The
Jellyfin exporter logs its HTTP scrape requests, so a healthy, regularly
scraped exporter should not require a longer fallback window.
