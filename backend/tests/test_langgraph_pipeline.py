"""Integration test: runs the full 8-agent LangGraph pipeline end-to-end
against LocalStack (a real AWS emulator, reached over HTTP) instead of mocks.

Requires the `terraagent-localstack` container (docker-compose "test" profile):
    docker compose --profile test up -d terraagent-localstack
Skips itself automatically if LocalStack isn't reachable, so the rest of the
suite stays runnable without it.
"""

import os

import boto3
import httpx
import pytest

LOCALSTACK_URL = os.getenv("LOCALSTACK_URL", "http://localhost:4566")


def _localstack_reachable() -> bool:
    try:
        resp = httpx.get(f"{LOCALSTACK_URL}/_localstack/health", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _localstack_reachable(), reason="LocalStack is not reachable"),
]


@pytest.fixture
def localstack_resources():
    """Creates a small EC2 + VPC + S3 footprint in LocalStack for the pipeline to discover."""
    session = boto3.Session(
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )
    ec2 = session.client("ec2", endpoint_url=LOCALSTACK_URL)
    s3 = session.client("s3", endpoint_url=LOCALSTACK_URL)

    vpc = ec2.create_vpc(CidrBlock="10.50.0.0/16")["Vpc"]
    ec2.create_subnet(VpcId=vpc["VpcId"], CidrBlock="10.50.1.0/24")
    ec2.create_security_group(VpcId=vpc["VpcId"], GroupName="terraagent-test-sg", Description="test sg")

    images = ec2.describe_images().get("Images", [])
    if images:
        ec2.run_instances(ImageId=images[0]["ImageId"], MinCount=1, MaxCount=1, InstanceType="t2.micro")

    s3.create_bucket(Bucket="terraagent-integration-test-bucket")

    return {"vpc_id": vpc["VpcId"]}


@pytest.mark.asyncio
async def test_full_pipeline_against_localstack(localstack_resources):
    from agents.graph import build_graph

    graph = build_graph()
    initial_state = {
        "job_id": "job-integration-test",
        "created_at": "2026-01-01T00:00:00",
        "operation": "generate",
        "region": "us-east-1",
        "resource_filters": ["VPC", "EC2", "S3", "SG"],
        "aws_credentials": {
            "access_key": "test",
            "secret_key": "test",
            "session_token": None,
        },
        "aws_endpoint_url": LOCALSTACK_URL,
        "terraform_binary": "terraform",
        "run_plan_equivalence": False,
        "intent": {},
        "resources": [],
        "classification_results": {},
        "dependency_graph": {},
        "adoption_plan": {},
        "terraform_files": {},
        "validation_results": {},
        "drift_results": {},
        "plan_equivalence_results": {},
        "security_results": {},
        "cost_results": {},
        "repair_attempts": 0,
        "repair_risk_tier": None,
        "pending_approval": None,
        "approval_decision": None,
        "documentation": {},
        "github_pr": None,
        "github_wave_prs": {},
        "zip_path": None,
        "zip_sha256": None,
        "zip_manifest": [],
        "errors": [],
        "status": "RUNNING",
        "completed_agents": [],
        "current_agent": "intent_router",
        "progress_percentage": 0,
        "agent_timings": {},
    }

    final_state = await graph.ainvoke(initial_state)

    assert len(final_state["resources"]) > 0
    # terraform_composer now splits resource output across stack-based files
    # (foundation.tf/security.tf/data.tf/application.tf) instead of one fixed
    # resources.tf - assert at least one actually landed, not a specific name.
    STACK_FILE_NAMES = {"foundation.tf", "security.tf", "data.tf", "application.tf"}
    assert STACK_FILE_NAMES & final_state["terraform_files"].keys()
    assert final_state["validation_results"]["passed"] is True
    assert final_state["zip_path"]
    assert os.path.exists(final_state["zip_path"])

    import zipfile
    assert zipfile.is_zipfile(final_state["zip_path"])
