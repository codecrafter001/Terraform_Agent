"""Router for AWS Organizations account enumeration (multi-account discovery
support). See tools/aws_organizations.py for the scope/design note - this
lists accounts so an operator can then kick off one POST /api/scan per
account_id via role_arn; it does not itself fan a scan out across accounts."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from models.organizations import OrganizationAccount, OrganizationAccountsRequest, OrganizationAccountsResponse
from services.auth import require_api_key
from tools.aws_organizations import list_organization_accounts
from tools.credential_scrubber import CredentialScrubber

logger = logging.getLogger("terraagent.routers.organizations")

router = APIRouter(prefix="/organizations", tags=["organizations"], dependencies=[Depends(require_api_key)])


@router.post("/accounts", response_model=OrganizationAccountsResponse)
async def get_organization_accounts(request: OrganizationAccountsRequest):
    """Lists every account in the caller's AWS Organization."""
    try:
        accounts = await asyncio.to_thread(
            list_organization_accounts,
            access_key=request.aws_access_key.get_secret_value(),
            secret_key=request.aws_secret_key.get_secret_value(),
            session_token=request.aws_session_token.get_secret_value() if request.aws_session_token else None,
            management_role_arn=request.management_role_arn,
            region=request.region
        )
    except RuntimeError as e:
        scrubbed = CredentialScrubber.scrub_text(str(e))
        logger.warning(f"Organization account listing failed: {scrubbed}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=scrubbed)

    return OrganizationAccountsResponse(accounts=[OrganizationAccount(**a) for a in accounts])
