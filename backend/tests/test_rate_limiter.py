"""Regression coverage for services/rate_limiter.py::get_client_ip.

Guards against a real, verified bypass: nginx.conf sets X-Forwarded-For via
$proxy_add_x_forwarded_for, which APPENDS its own observed IP to whatever the
client already sent rather than replacing it - so trusting the FIRST entry
(the previous implementation) let an attacker spoof a different apparent
client on every request, defeating the per-IP scan rate limit even with
Nginx as the only path into the API. X-Real-IP is different: nginx sets it
via plain $remote_addr, which fully replaces the header rather than
appending - nothing in the request can override it.
"""

from services.rate_limiter import get_client_ip


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, headers, client_host="10.0.0.5"):
        self.headers = headers
        self.client = _FakeClient(client_host)


def test_prefers_x_real_ip_over_x_forwarded_for():
    # A spoofed X-Forwarded-For must never win over a genuine X-Real-IP.
    req = _FakeRequest({"X-Real-IP": "203.0.113.50", "X-Forwarded-For": "1.2.3.4, 203.0.113.50"})
    assert get_client_ip(req) == "203.0.113.50"


def test_spoofed_x_forwarded_for_first_entry_does_not_change_resolved_ip():
    # Nginx appends its own observed IP to the end of whatever the client
    # sent - the attacker-controlled part is always the first entry, never
    # the last. Three different spoofed first entries, same real last entry,
    # must all resolve identically.
    for spoofed in ["1.2.3.0", "1.2.3.1", "9.9.9.9"]:
        req = _FakeRequest({"X-Forwarded-For": f"{spoofed}, 203.0.113.50"})
        assert get_client_ip(req) == "203.0.113.50"


def test_falls_back_to_request_client_host_with_no_headers():
    req = _FakeRequest({}, client_host="10.0.0.5")
    assert get_client_ip(req) == "10.0.0.5"


def test_falls_back_to_x_forwarded_for_last_entry_when_x_real_ip_absent():
    req = _FakeRequest({"X-Forwarded-For": "1.2.3.4, 203.0.113.50"})
    assert get_client_ip(req) == "203.0.113.50"
