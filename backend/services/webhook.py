"""Optional webhook notification: POSTs a scan result summary to a
user-configured URL once a job reaches a terminal state (COMPLETE or
FAILED). Best-effort - a webhook delivery failure must never fail the scan
job itself, since the job's own work is already done by the time this runs.
"""

import ipaddress
import logging
import socket
from typing import Any, Dict, List
from urllib.parse import urlparse, urlunparse

import httpx

logger = logging.getLogger("terraagent.webhook")


class UnsafeWebhookURLError(ValueError):
    pass


def _safe_resolved_ips(url: str) -> List[str]:
    """webhook_url is attacker-controlled input (any caller of POST /api/scan
    can set it) and this function's caller then makes a real HTTP request to
    it FROM the backend server. Without this check, TerraAgent becomes an
    SSRF proxy: a caller could set webhook_url to the cloud metadata endpoint
    (http://169.254.169.254/latest/meta-data/) or any internal-only service
    reachable from this container (e.g. http://terraagent-postgres:5432,
    http://terraagent-redis:6379) and use this server to probe or attack
    internal infrastructure it has no direct network access to.

    Blocks: non-http(s) schemes, loopback, link-local (incl. the cloud
    metadata range), private/RFC1918, and other reserved ranges - resolved
    from the actual hostname, not just pattern-matched against the literal
    string, so a private IP can't sneak through under a public-looking
    hostname.

    Returns the validated IPs rather than just pass/fail: send_scan_summary
    connects directly to one of these (via httpx's "sni_hostname" request
    extension, so HTTPS cert/SNI validation still checks the real hostname)
    instead of letting httpx re-resolve the hostname itself moments later.
    Without that, an attacker controlling DNS for their webhook's hostname
    could return a safe IP here, then swap the DNS record to an internal
    address before httpx's own connect-time lookup runs - a classic
    DNS-rebinding bypass of exactly this kind of validate-then-fetch check.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeWebhookURLError(f"webhook_url must be http or https, got: {parsed.scheme!r}")
    if not parsed.hostname:
        raise UnsafeWebhookURLError("webhook_url has no hostname")

    try:
        resolved_addrs = {str(info[4][0]) for info in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror as e:
        raise UnsafeWebhookURLError(f"webhook_url hostname does not resolve: {e}") from e

    for addr in resolved_addrs:
        ip = ipaddress.ip_address(addr)
        if (
            ip.is_loopback or ip.is_link_local or ip.is_private
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise UnsafeWebhookURLError(
                f"webhook_url resolves to a non-public address ({addr}) - refusing to avoid SSRF"
            )
    return list(resolved_addrs)


async def send_scan_summary(webhook_url: str, job_id: str, final_state: Dict[str, Any]) -> None:
    try:
        safe_ips = _safe_resolved_ips(webhook_url)
    except UnsafeWebhookURLError as e:
        logger.warning(f"[{job_id}] Refusing to deliver webhook: {e}")
        return

    resources = final_state.get("resources", []) or []
    validation = final_state.get("validation_results", {}) or {}
    security = final_state.get("security_results", {}) or {}

    payload = {
        "job_id": job_id,
        "status": final_state.get("status", "COMPLETE"),
        "region": final_state.get("region"),
        "resources_discovered": len(resources),
        "validation_passed": bool(validation.get("passed")),
        "security_findings_count": len((security.get("findings") or [])),
        "security_risk_score": security.get("risk_score"),
        "zip_available": bool(final_state.get("zip_path")),
        "zip_sha256": final_state.get("zip_sha256")
    }

    # Connect directly to the address we already validated instead of the
    # original hostname, which httpx would otherwise re-resolve itself right
    # before connecting - see _safe_resolved_ips' docstring for why that gap
    # matters. sni_hostname (an httpx/httpcore request extension) keeps HTTPS
    # working correctly against a raw IP: it drives both the TLS SNI value and
    # certificate hostname verification, and the explicit Host header covers
    # HTTP virtual-hosting the same way.
    parsed = urlparse(webhook_url)
    pinned_ip = safe_ips[0]
    netloc = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    if parsed.port:
        netloc += f":{parsed.port}"
    pinned_url = urlunparse(parsed._replace(netloc=netloc))

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(
                pinned_url,
                json=payload,
                headers={"Host": parsed.netloc},
                extensions={"sni_hostname": parsed.hostname},
            )
            response.raise_for_status()
        logger.info(f"[{job_id}] Webhook delivered to {webhook_url} ({response.status_code})")
    except Exception as e:
        # Never raise - a broken/unreachable webhook endpoint is the caller's
        # problem, not a reason to mark an otherwise-successful scan as failed.
        logger.warning(f"[{job_id}] Webhook delivery to {webhook_url} failed: {e}")
