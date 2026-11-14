# ABOUTME: Validates privacy, codec mapping, and failure behavior for the
# repository-owned Jellyfin Sessions API Prometheus exporter.

import importlib.util
import io
import unittest
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
EXPORTER_PATH = REPO / "overlay-root/usr/share/nas/jellyfin-exporter/jellyfin_exporter.py"
SPEC = importlib.util.spec_from_file_location("jellyfin_exporter", EXPORTER_PATH)
EXPORTER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(EXPORTER)


TRANSCODING_SESSION = {
    "Id": "sensitive-session-id",
    "UserName": "private-user",
    "RemoteEndPoint": "192.0.2.10",
    "Client": "Jellyfin Web",
    "DeviceName": "Phone",
    "PlayState": {
        "PlayMethod": "Transcode",
        "AudioStreamIndex": 2,
        "IsPaused": False,
        "PositionTicks": 600_000_000,
    },
    "NowPlayingItem": {
        "Name": 'Pilot "Episode"',
        "SeriesName": "Example Show",
        "ParentIndexNumber": 1,
        "IndexNumber": 1,
        "Container": "mkv",
        "RunTimeTicks": 6_000_000_000,
        "MediaStreams": [
            {
                "Index": 0,
                "Type": "Video",
                "Codec": "hevc",
                "Width": 3840,
                "Height": 2160,
                "RealFrameRate": 23.75,
            },
            {"Index": 1, "Type": "Audio", "Codec": "aac", "Channels": 2},
            {"Index": 2, "Type": "Audio", "Codec": "truehd", "Channels": 8},
        ],
    },
    "TranscodingInfo": {
        "AudioCodec": "aac",
        "VideoCodec": "h264",
        "Container": "ts",
        "IsVideoDirect": False,
        "IsAudioDirect": False,
        "Bitrate": 8_000_000,
        "Framerate": 47.5,
        "CompletionPercentage": 12.25,
        "Width": 1920,
        "Height": 1080,
        "HardwareAccelerationType": "none",
        "TranscodeReasons": "VideoCodecNotSupported, AudioCodecNotSupported",
    },
}


DIRECT_SESSION = {
    "Client": "Android TV",
    "DeviceName": "Living Room",
    "PlayState": {"PlayMethod": "DirectPlay", "AudioStreamIndex": 1, "IsPaused": True},
    "NowPlayingItem": {
        "Name": "Direct Movie",
        "Container": "mkv",
        "MediaStreams": [
            {"Index": 0, "Type": "Video", "Codec": "av1", "Width": 1920, "Height": 1080},
            {"Index": 1, "Type": "Audio", "Codec": "opus"},
        ],
    },
}


class JellyfinExporterTests(unittest.TestCase):
    def test_sessions_query_uses_mediabrowser_authorization(self):
        with (
            patch.object(EXPORTER, "API_KEY_FILE") as key_file,
            patch.object(EXPORTER, "urlopen", return_value=io.BytesIO(b"[]")) as open_url,
        ):
            key_file.read_text.return_value = "test-api-key\n"

            self.assertEqual(EXPORTER.query_sessions(), [])

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, f"{EXPORTER.JELLYFIN_URL}/Sessions")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), 'MediaBrowser Token="test-api-key"')
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertIsNone(request.get_header("X-emby-token"))
        self.assertEqual(open_url.call_args.kwargs["timeout"], EXPORTER.API_TIMEOUT_SECONDS)

    def test_authorization_token_is_encoded_as_a_quoted_parameter(self):
        with (
            patch.object(EXPORTER, "API_KEY_FILE") as key_file,
            patch.object(EXPORTER, "urlopen", return_value=io.BytesIO(b"[]")) as open_url,
        ):
            key_file.read_text.return_value = 'key"with,parameters'
            EXPORTER.query_sessions()

        request = open_url.call_args.args[0]
        self.assertEqual(
            request.get_header("Authorization"),
            'MediaBrowser Token="key%22with%2Cparameters"',
        )

    def test_transcode_exposes_source_target_and_diagnostic_values(self):
        rendered = EXPORTER.render_metrics([TRANSCODING_SESSION])

        self.assertIn('play_method="Transcode"', rendered)
        self.assertIn('source_video_codec="hevc"', rendered)
        self.assertIn('source_audio_codec="truehd"', rendered)
        self.assertIn('source_resolution="3840x2160"', rendered)
        self.assertIn('target_video_codec="h264"', rendered)
        self.assertIn('target_audio_codec="aac"', rendered)
        self.assertIn('target_resolution="1920x1080"', rendered)
        self.assertIn('transcode_reasons="VideoCodecNotSupported, AudioCodecNotSupported"', rendered)
        self.assertIn("jellyfin_transcode_bitrate_bits_per_second", rendered)
        self.assertIn(" 8000000", rendered)
        self.assertIn("jellyfin_transcode_framerate", rendered)
        self.assertIn(" 47.5", rendered)
        self.assertIn("jellyfin_transcode_speed_ratio", rendered)
        self.assertIn(" 2.0", rendered)
        self.assertIn("jellyfin_playback_position_seconds", rendered)
        self.assertIn(" 60.0", rendered)
        self.assertIn("jellyfin_playback_progress_percent", rendered)
        self.assertIn(" 10.0", rendered)

    def test_direct_play_reports_copy_semantics_and_pause_state(self):
        rendered = EXPORTER.render_metrics([DIRECT_SESSION])

        self.assertIn('play_method="DirectPlay"', rendered)
        self.assertIn('paused="true"', rendered)
        self.assertIn('source_video_codec="av1"', rendered)
        self.assertIn('target_video_codec="av1"', rendered)
        self.assertIn('video_direct="true"', rendered)
        self.assertIn('target_container="mkv"', rendered)

    def test_idle_sessions_count_but_do_not_create_playback_series(self):
        rendered = EXPORTER.render_metrics([{"Client": "Idle"}])

        self.assertIn("jellyfin_sessions_total 1", rendered)
        self.assertIn("jellyfin_playback_streams_active 0", rendered)
        self.assertNotIn("jellyfin_playback_info{", rendered)

    def test_sensitive_user_and_network_fields_are_not_exported(self):
        rendered = EXPORTER.render_metrics([TRANSCODING_SESSION])

        self.assertNotIn("private-user", rendered)
        self.assertNotIn("192.0.2.10", rendered)
        self.assertNotIn("sensitive-session-id", rendered)
        self.assertRegex(rendered, r'stream_id="[0-9a-f]{12}"')

    def test_label_values_are_prometheus_escaped(self):
        rendered = EXPORTER.render_metrics([TRANSCODING_SESSION])

        self.assertIn('title="Pilot \\"Episode\\""', rendered)

    def test_failed_query_is_scrapeable_and_reports_down(self):
        rendered = EXPORTER.render_metrics([], exporter_up=False, error="HTTPError: token rejected")

        self.assertIn("jellyfin_exporter_up 0", rendered)
        self.assertIn('error="HTTPError: token rejected"', rendered)


if __name__ == "__main__":
    unittest.main()
