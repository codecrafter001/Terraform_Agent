"""Security test: the webhook feature must never let a caller-supplied
webhook_url turn the backend into an SSRF proxy against internal
infrastructure or the cloud metadata endpoint."""

import pytest
from services.webhook import _safe_resolved_ips, UnsafeWebhookURLError


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
        "http://127.0.0.1/",                          # loopback
        "http://localhost:8000/",                      # loopback via hostname
        "http://10.0.0.5/",                            # RFC1918 private
        "http://172.16.0.1/",                          # RFC1918 private
        "http://192.168.1.1/",                         # RFC1918 private
        "http://[::1]/",                                # IPv6 loopback
    ],
)
def test_rejects_internal_and_metadata_targets(url):
    with pytest.raises(UnsafeWebhookURLError):
        _safe_resolved_ips(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://example.com/", "ftp://example.com/"])
def test_rejects_non_http_schemes(url):
    with pytest.raises(UnsafeWebhookURLError):
        _safe_resolved_ips(url)


def test_allows_public_looking_url():
    # example.com resolves to a public IP - must not be blocked, and the
    # validated IP(s) must actually be returned for send_scan_summary to pin
    # its connection to (see _safe_resolved_ips' docstring for why).
    ips = _safe_resolved_ips("https://example.com/webhook")
    assert ips
