"""Shared-secret API key check for every endpoint that touches AWS
credentials, discovered infrastructure data, or generated output.

Before this existed, the API had no authentication at all: anyone able to
reach it could submit arbitrary AWS credentials to trigger a scan (burning
the account owner's AWS API quota and LLM cost) and, since job IDs carry no
ownership check, download the full discovered inventory + generated
Terraform for any job whose ID they'd seen (a shared link, a proxy log, a
referrer header) with nothing else required.

Off by default - mirrors this codebase's existing "opt-in hardening" pattern
(role_arn, webhook_url, zip_password all default to unset/off) so shipping
this doesn't immediately break a deployment that hasn't set the key yet. The
moment TERRAAGENT_API_KEY is set, every protected router starts enforcing it;
main.py's startup logs a loud warning while it's unset, since running with
no auth at all is the exact gap this closes.
"""

import logging
import os
import secrets

from fastapi import Header, HTTPException, status

logger = logging.getLogger("terraagent.auth")

_API_KEY = os.getenv("TERRAAGENT_API_KEY")


async def require_api_key(x_api_key: str = Header(default=None)) -> None:
    if not _API_KEY:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, _API_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")


def warn_if_unset() -> None:
    if not _API_KEY:
        logger.warning(
            "TERRAAGENT_API_KEY is not set - the API is running with NO authentication. "
            "Anyone who can reach it can submit AWS credentials and download any job's "
            "discovered inventory. Set TERRAAGENT_API_KEY before exposing this beyond a "
            "fully trusted, closed network."
        )
