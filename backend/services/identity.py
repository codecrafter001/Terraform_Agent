"""User identity for deployment mode (design doc §4 D3, task 3.7).

The approver's identity is what the approval gate and AWS CloudTrail
(SourceIdentity) record, so it must come from something the caller cannot
forge. Two trusted sources, chosen by the operator - never by the request:

1. TERRAAGENT_TRUSTED_PROXY_AUTH=true: an authenticating reverse proxy in
   front of the API (oauth2-proxy, AWS ALB OIDC, Cloudflare Access) sets
   X-Forwarded-Email / X-Auth-Request-Email. Only enable this when that proxy
   strips any copy of these headers sent by the client - otherwise anyone can
   claim to be anyone.
2. TERRAAGENT_OPERATOR_EMAIL=<email>: a single-operator install. Every
   request acts as this one person, set server-side.

Without either, there is no identity and approve/deploy are refused (401).
Identity is never derived from the API key, a bearer token or any other
header - those either leak part of a secret into the audit trail or are
freely settable by the caller.
"""

import os
import re
from typing import Optional

from fastapi import HTTPException, Request, status

_TRUSTED_EMAIL_HEADERS = (
    "X-Forwarded-Email",
    "X-Auth-Request-Email",
    "X-Forwarded-User",
    "X-Auth-Request-User",
)
_TRUSTED_TENANT_HEADERS = (
    "X-Forwarded-Tenant",
    "X-Auth-Request-Tenant",
    "X-Tenant-Id",
)
_IDENTITY = re.compile(r"^[A-Za-z0-9._%+@-]{1,254}$")


def _trusted_proxy() -> bool:
    return os.getenv("TERRAAGENT_TRUSTED_PROXY_AUTH", "").strip().lower() == "true"


def _clean(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip().lower()
    return value if value and _IDENTITY.match(value) else None


def current_user(request: Optional[Request] = None) -> Optional[str]:
    """The authenticated user's email/identifier, or None."""
    if _trusted_proxy():
        if request is None:
            return None
        for header in _TRUSTED_EMAIL_HEADERS:
            user = _clean(request.headers.get(header))
            if user:
                return user
        return None
    return _clean(os.getenv("TERRAAGENT_OPERATOR_EMAIL"))


def current_tenant(request: Optional[Request] = None) -> Optional[str]:
    """The caller's tenant, or None for a single-tenant install (no filtering).
    Tenant headers are only trusted behind the authenticating proxy."""
    if not _trusted_proxy() or request is None:
        return None
    for header in _TRUSTED_TENANT_HEADERS:
        tenant = _clean(request.headers.get(header))
        if tenant:
            return tenant
    user = current_user(request)
    if user and "@" in user:
        return user.split("@", 1)[1]
    return user


def require_authenticated_user(request: Optional[Request] = None) -> str:
    """The authenticated user, or 401 explaining how to configure identity."""
    user = current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Approving or deploying needs a verified identity. Set TERRAAGENT_OPERATOR_EMAIL on the API "
                "(single operator), or run behind an SSO proxy and set TERRAAGENT_TRUSTED_PROXY_AUTH=true."
            ),
        )
    return user


def require_tenant(request: Optional[Request] = None) -> str:
    """Ensures a tenant identity is present on the request."""
    tenant = current_tenant(request)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A verified tenant identity is required for this action.",
        )
    return tenant
