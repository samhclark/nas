#!/usr/bin/env python3
# ABOUTME: Exposes privacy-bounded Jellyfin playback and transcode metrics by
# polling the authenticated Sessions API on each Prometheus scrape.

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9594"))
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://127.0.0.1:8096").rstrip("/")
API_KEY_FILE = Path(os.environ.get("JELLYFIN_API_KEY_FILE", "/run/secrets/jellyfin-api-key"))
API_TIMEOUT_SECONDS = float(os.environ.get("JELLYFIN_API_TIMEOUT_SECONDS", "5"))


def label_escape(value: Any) -> str:
    return str(value if value is not None else "").replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def labels(values: dict[str, Any]) -> str:
    return ",".join(f'{key}="{label_escape(value)}"' for key, value in values.items())


def metric(name: str, value: int | float, metric_labels: dict[str, Any] | None = None) -> str:
    suffix = f"{{{labels(metric_labels)}}}" if metric_labels else ""
    return f"{name}{suffix} {value}"


def selected_stream(item: dict[str, Any], stream_type: str, selected_index: int | None = None) -> dict[str, Any]:
    streams = [stream for stream in item.get("MediaStreams", []) if stream.get("Type") == stream_type]
    if selected_index is not None:
        for stream in streams:
            if stream.get("Index") == selected_index:
                return stream
    return streams[0] if streams else {}


def resolution(stream: dict[str, Any]) -> str:
    width = stream.get("Width")
    height = stream.get("Height")
    return f"{width}x{height}" if width and height else "unknown"


def active_playback(session: dict[str, Any]) -> dict[str, Any] | None:
    item = session.get("NowPlayingItem")
    state = session.get("PlayState")
    if not item or not state:
        return None

    video = selected_stream(item, "Video")
    audio = selected_stream(item, "Audio", state.get("AudioStreamIndex"))
    transcode = session.get("TranscodingInfo") or {}
    play_method = state.get("PlayMethod") or ("Transcode" if transcode else "Unknown")
    source_container = item.get("Container") or "unknown"
    source_video = video.get("Codec") or "none"
    source_audio = audio.get("Codec") or "none"
    stream_identity = session.get("Id") or "\x1f".join(
        str(value or "")
        for value in (
            session.get("DeviceId"),
            session.get("Client"),
            session.get("DeviceName"),
            item.get("Id"),
        )
    )
    stream_id = hashlib.sha256(stream_identity.encode("utf-8")).hexdigest()[:12]

    if transcode:
        target_video = source_video if transcode.get("IsVideoDirect") else (transcode.get("VideoCodec") or "unknown")
        target_audio = source_audio if transcode.get("IsAudioDirect") else (transcode.get("AudioCodec") or "unknown")
        target_container = transcode.get("Container") or "unknown"
        target_resolution = (
            f'{transcode["Width"]}x{transcode["Height"]}'
            if transcode.get("Width") and transcode.get("Height")
            else "unknown"
        )
    elif play_method == "DirectPlay":
        target_video = source_video
        target_audio = source_audio
        target_container = source_container
        target_resolution = resolution(video)
    else:
        target_video = source_video if play_method == "DirectStream" else "unknown"
        target_audio = source_audio if play_method == "DirectStream" else "unknown"
        target_container = "unknown"
        target_resolution = resolution(video) if play_method == "DirectStream" else "unknown"

    reasons = transcode.get("TranscodeReasons")
    if isinstance(reasons, list):
        reasons = ", ".join(str(reason) for reason in reasons)

    return {
        "labels": {
            "stream_id": stream_id,
            "title": item.get("Name") or "unknown",
            "series": item.get("SeriesName") or "",
            "season": item.get("ParentIndexNumber") or "",
            "episode": item.get("IndexNumber") or "",
            "client": session.get("Client") or "unknown",
            "device": session.get("DeviceName") or "unknown",
            "play_method": play_method,
            "paused": str(bool(state.get("IsPaused"))).lower(),
            "source_video_codec": source_video,
            "source_audio_codec": source_audio,
            "source_container": source_container,
            "source_resolution": resolution(video),
            "target_video_codec": target_video,
            "target_audio_codec": target_audio,
            "target_container": target_container,
            "target_resolution": target_resolution,
            "video_direct": str(bool(transcode.get("IsVideoDirect", not transcode))).lower(),
            "audio_direct": str(bool(transcode.get("IsAudioDirect", not transcode))).lower(),
            "hardware_acceleration": transcode.get("HardwareAccelerationType") or "none",
            "transcode_reasons": reasons or "none",
        },
        "transcoding": play_method == "Transcode" or bool(transcode),
        "bitrate": transcode.get("Bitrate"),
        "framerate": transcode.get("Framerate"),
        "completion": transcode.get("CompletionPercentage"),
        "source_framerate": video.get("RealFrameRate") or video.get("AverageFrameRate"),
        "position_seconds": (state.get("PositionTicks") or 0) / 10_000_000,
        "duration_seconds": (item.get("RunTimeTicks") or 0) / 10_000_000,
    }


def render_metrics(sessions: list[dict[str, Any]], exporter_up: bool = True, error: str = "") -> str:
    playbacks = [playback for session in sessions if (playback := active_playback(session)) is not None]
    methods = Counter(playback["labels"]["play_method"] for playback in playbacks)
    transcodes = [playback for playback in playbacks if playback["transcoding"]]
    lines = [
        "# HELP jellyfin_exporter_up Whether the exporter successfully queried Jellyfin's Sessions API.",
        "# TYPE jellyfin_exporter_up gauge",
        metric("jellyfin_exporter_up", int(exporter_up)),
        "# HELP jellyfin_exporter_last_scrape_error_info The most recent scrape error, present only when Jellyfin could not be queried.",
        "# TYPE jellyfin_exporter_last_scrape_error_info gauge",
    ]
    if error:
        lines.append(metric("jellyfin_exporter_last_scrape_error_info", 1, {"error": error[:160]}))
    lines += [
        "# HELP jellyfin_sessions_total Sessions currently known to Jellyfin, including idle clients.",
        "# TYPE jellyfin_sessions_total gauge",
        metric("jellyfin_sessions_total", len(sessions)),
        "# HELP jellyfin_playback_streams_active Sessions currently playing or paused on an item.",
        "# TYPE jellyfin_playback_streams_active gauge",
        metric("jellyfin_playback_streams_active", len(playbacks)),
        "# HELP jellyfin_transcodes_active Sessions with active transcoding information.",
        "# TYPE jellyfin_transcodes_active gauge",
        metric("jellyfin_transcodes_active", len(transcodes)),
        "# HELP jellyfin_playback_streams_by_method Active playback sessions by Jellyfin play method.",
        "# TYPE jellyfin_playback_streams_by_method gauge",
    ]
    for method_name in ("DirectPlay", "DirectStream", "Transcode", "Unknown"):
        lines.append(metric("jellyfin_playback_streams_by_method", methods[method_name], {"play_method": method_name}))

    lines += [
        "# HELP jellyfin_playback_info Current playback details; excludes usernames and remote addresses by design.",
        "# TYPE jellyfin_playback_info gauge",
        "# HELP jellyfin_transcode_bitrate_bits_per_second Current requested transcode bitrate.",
        "# TYPE jellyfin_transcode_bitrate_bits_per_second gauge",
        "# HELP jellyfin_transcode_framerate Current transcode processing rate in frames per second.",
        "# TYPE jellyfin_transcode_framerate gauge",
        "# HELP jellyfin_transcode_completion_percent Current transcode completion percentage.",
        "# TYPE jellyfin_transcode_completion_percent gauge",
        "# HELP jellyfin_transcode_speed_ratio Transcode FPS divided by source FPS; values below one cannot sustain real-time playback.",
        "# TYPE jellyfin_transcode_speed_ratio gauge",
        "# HELP jellyfin_playback_position_seconds Last position reported by the playback client.",
        "# TYPE jellyfin_playback_position_seconds gauge",
        "# HELP jellyfin_playback_progress_percent Last reported playback position as a percentage of item duration.",
        "# TYPE jellyfin_playback_progress_percent gauge",
    ]
    for playback in playbacks:
        playback_labels = playback["labels"]
        lines.append(metric("jellyfin_playback_info", 1, playback_labels))
        detail_labels = {
            "stream_id": playback_labels["stream_id"],
            "title": playback_labels["title"],
            "client": playback_labels["client"],
            "device": playback_labels["device"],
        }
        if playback["bitrate"] is not None:
            lines.append(metric("jellyfin_transcode_bitrate_bits_per_second", playback["bitrate"], detail_labels))
        if playback["framerate"] is not None:
            lines.append(metric("jellyfin_transcode_framerate", playback["framerate"], detail_labels))
        if playback["completion"] is not None:
            lines.append(metric("jellyfin_transcode_completion_percent", playback["completion"], detail_labels))
        if playback["framerate"] is not None and playback["source_framerate"]:
            lines.append(
                metric(
                    "jellyfin_transcode_speed_ratio",
                    playback["framerate"] / playback["source_framerate"],
                    detail_labels,
                )
            )
        lines.append(metric("jellyfin_playback_position_seconds", playback["position_seconds"], detail_labels))
        if playback["duration_seconds"]:
            lines.append(
                metric(
                    "jellyfin_playback_progress_percent",
                    100 * playback["position_seconds"] / playback["duration_seconds"],
                    detail_labels,
                )
            )
    return "\n".join(lines) + "\n"


def query_sessions() -> list[dict[str, Any]]:
    token = API_KEY_FILE.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(f"API key file is empty: {API_KEY_FILE}")
    request = Request(
        f"{JELLYFIN_URL}/Sessions",
        headers={
            "Authorization": f'MediaBrowser Token="{quote(token, safe="")}"',
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise RuntimeError("Jellyfin Sessions API returned a non-list response")
    return payload


def safe_metrics() -> tuple[str, bool]:
    try:
        return render_metrics(query_sessions()), True
    except (HTTPError, URLError, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(error, file=sys.stderr, flush=True)
        return render_metrics([], exporter_up=False, error=error), False


class MetricsHandler(BaseHTTPRequestHandler):
    server_version = "jellyfin-exporter/1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/metrics":
            body, _ = safe_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        elif self.path == "/health":
            body, healthy = safe_metrics()
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            body = "Healthy\n" if healthy else "Unhealthy\n"
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            body = "Not Found\n"
        encoded = body.encode("utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format_string % args}", file=sys.stderr, flush=True)


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), MetricsHandler)
    print(f"Listening on http://{LISTEN_HOST}:{LISTEN_PORT}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
