# Jellyfin Playback Monitoring

This deployment polls Jellyfin's authenticated `/Sessions` API through a
small repository-owned exporter. VictoriaMetrics scrapes the exporter every
15 seconds and Grafana provisions the **Jellyfin Playback** dashboard from the
bootc image.

The dashboard is intended to answer the immediate operator question: why is
this playback working, buffering, frozen, or stuttering? It shows the current
play method, source and target codecs, resolutions, transcode reason, bitrate,
framerate, real-time transcode ratio, playback position, Jellyfin health
latency, and host CPU/memory pressure.

## Privacy and retention

The exporter includes Jellyfin usernames, current titles, clients, and device
names as metric labels to identify playback and diagnose streams. These labels
remain in VictoriaMetrics until its normal retention removes them. The exporter
deliberately omits remote addresses.

Do not add API keys, remote addresses, playback tokens, or media
paths to metric labels. Historical viewing reports belong in Jellyfin's
Playback Reporting plugin rather than operational time-series labels.

## One-time API key bootstrap

In Jellyfin, open **Dashboard → API Keys**, create a key named
`playback-metrics`, and copy it. On the development machine, edit the encrypted
secret file:

```bash
sops overlay-root/usr/share/nas/secrets/secrets.sops.yaml
```

Add this top-level value and save:

```yaml
jellyfin-api-key: <the-new-key>
```

Never commit the plaintext key. `generate-quadlets.py` verifies that every
declared runtime secret has an encrypted top-level key in the SOPS document.
At boot, `sops-distribute-secrets.service` writes it as:

```text
/run/nas-secrets/jellyfin-exporter/jellyfin-api-key
```

The rootless exporter mounts that file read-only at
`/run/secrets/jellyfin-api-key`.

## Deployment checks

After the image containing this service boots, verify the generated account
and user service:

```bash
getent passwd _nas_jellyfinmetrics
grep '^_nas_jellyfinmetrics:' /etc/subuid /etc/subgid
sudo systemctl status ensure-nas-jellyfinmetrics-account.service --no-pager
sudo systemctl status sops-distribute-secrets.service --no-pager
sudo systemctl --machine _nas_jellyfinmetrics@ --user status jellyfin-exporter.service --no-pager
```

The service should be active under the `krun` runtime. Its only listeners and
dependencies are loopback TCP 9594, loopback Jellyfin TCP 8096, the read-only
exporter source, and the read-only API key.

Verify the exporter without displaying sensitive values:

```bash
curl -fsS http://127.0.0.1:9594/health
curl -fsS http://127.0.0.1:9594/metrics | \
  grep -E '^jellyfin_(exporter_up|sessions_total|playback_streams_active|transcodes_active)'
```

Expected idle output includes `jellyfin_exporter_up 1`. The exporter returns a
scrapeable `jellyfin_exporter_up 0` metric when Jellyfin is unavailable or the
API key is rejected, while `/health` returns an error status.

Verify VictoriaMetrics ingestion:

```bash
curl -G -fsS http://127.0.0.1:8428/api/v1/query \
  --data-urlencode 'query=jellyfin_exporter_up{job="jellyfin-exporter"}' | jq .
```

Open Grafana and select **Jellyfin Playback**. During a representative
playback, confirm:

- the current-playback table shows the username and identifies Direct Play, Direct Stream, or
  Transcode
- source and target codecs match Jellyfin's playback-information UI
- a transcode faster than real time remains above `1.0x`
- the playback-position line advances while `Paused` is false
- health latency and host pressure remain responsive

## Interpretation limits

The Sessions API does not expose a client's actual buffered duration or
network throughput. A transcode ratio below `1.0x`, a flat unpaused playback
position, high host pressure, or increasing health latency are strong
correlations, not a definitive client-buffer measurement.

For Direct Stream, Jellyfin reports that the media codecs are copied but does
not include the remuxed output container in the session object. The dashboard
therefore shows the target container as `unknown` instead of guessing.

## Failure isolation

Jellyfin availability remains based on the independent blackbox probe of
`/health`. A failed exporter or expired API key must not page as though the
media server itself is down. `JellyfinPlaybackExporterBroken` is a separate
warning that fires only after playback telemetry has been unavailable for 15
minutes.
