# ABOUTME: Regression tests for private *arr management ingress in Caddy.

import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CADDYFILE = (
    REPO / "overlay-root/usr/share/nas/caddy/Caddyfile"
).read_text()


ARR_SITES = {
    "sonarr.i.samhclark.com": "sonarr.krun:8989",
    "radarr.i.samhclark.com": "radarr.krun:7878",
    "prowlarr.i.samhclark.com": "prowlarr.krun:9696",
    "sabnzbd.i.samhclark.com": "sabnzbd.krun:8080",
    "bazarr.i.samhclark.com": "bazarr.krun:6767",
}


def site_block(hostname):
    match = re.search(
        rf"(?m)^{re.escape(hostname)} \{{\n(?P<body>.*?)^\}}$",
        CADDYFILE,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing Caddy site block for {hostname}")
    return match.group("body")


class ArrIngressTests(unittest.TestCase):
    def test_private_ingress_snippet_allows_lan_and_tailscale_only(self):
        snippet = re.search(
            r"(?ms)^\(private-ingress\) \{\n(?P<body>.*?)^\}$",
            CADDYFILE,
        )
        self.assertIsNotNone(snippet)
        body = snippet.group("body")
        self.assertIn(
            "@private remote_ip private_ranges 100.64.0.0/10",
            body,
        )
        self.assertIn("handle @private", body)
        self.assertIn("reverse_proxy {args[0]}", body)
        self.assertIn("respond 403", body)

    def test_each_arr_ui_imports_the_guarded_backend(self):
        for hostname, backend in ARR_SITES.items():
            body = site_block(hostname)
            self.assertEqual(
                body.strip(),
                f"import private-ingress {backend}",
            )

    def test_arr_sites_do_not_use_direct_unguarded_reverse_proxies(self):
        for hostname in ARR_SITES:
            body = site_block(hostname)
            self.assertNotIn("reverse_proxy", body)
            self.assertIn("import private-ingress", body)


if __name__ == "__main__":
    unittest.main()
