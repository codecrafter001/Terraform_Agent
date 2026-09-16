"""Shared slowapi Limiter instance.

Defined in its own module (rather than in main.py) so both main.py (which
registers it on app.state and wires the exception handler) and
routers/scan.py (which applies the @limiter.limit(...) decorator) can import
it without a circular import between the two.
"""

from fastapi import Request
from slowapi import Limiter


# Deliberately NOT slowapi's own get_remote_address or get_ipaddr:
#
# - get_remote_address reads only request.client.host, the direct TCP peer.
#   docker-compose puts Nginx in front of the API on the intended path (port
#   80), so in that topology request.client.host is always Nginx's container
#   IP for every visitor - the "10 scans/hour per IP" limit would actually be
#   one shared global bucket, not per-client, defeating the point.
#
# - get_ipaddr is slowapi's own fix for exactly that, but it looks up the
#   header as request.headers["X_FORWARDED_FOR"] (underscore) - a WSGI/CGI
#   environ-style name. Starlette's Headers store the real hyphenated HTTP
#   header name ("X-Forwarded-For"), so that lookup never matches and
#   get_ipaddr silently falls back to request.client.host every time,
#   reproducing the exact same bug. (Confirmed directly: `"X_FORWARDED_FOR"
#   in Headers({"X-Forwarded-For": "1.2.3.4"})` is False.)
#
# A previous version of this trusted X-Forwarded-For's FIRST entry - a real,
# verified bypass: nginx.conf sets X-Forwarded-For via
# $proxy_add_x_forwarded_for, which APPENDS nginx's own observed IP to
# whatever the client already sent rather than replacing it. So an attacker
# could send their own X-Forwarded-For and nginx would just tack its
# (trustworthy) observation onto the END - the FIRST entry stayed
# attacker-controlled. Verified: a fixed client sending a different spoofed
# X-Forwarded-For per request was treated as a new client every time, even
# with port 8000 closed and Nginx as the only path in.
#
# X-Real-IP is different: nginx.conf sets it via plain `$remote_addr`
# (proxy_set_header X-Real-IP $remote_addr), which fully REPLACES the
# header value nginx forwards upstream - it does not preserve or append to
# anything the client sent. $remote_addr is nginx's own measurement of the
# actual TCP peer, taken from the socket connection itself, not a header -
# nothing in the request can override it. So prefer it; X-Forwarded-For's
# LAST entry (nginx's own append, not the attacker-controlled first one) and
# request.client.host remain as fallbacks for the local-dev-without-Nginx
# case, where neither header is set at all.
def get_client_ip(request: Request) -> str:
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


limiter = Limiter(key_func=get_client_ip)
