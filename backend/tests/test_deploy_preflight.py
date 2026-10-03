"""VPC quota pre-check (deploy/preflight.py): read-only calls, a clear message when
one more VPC wouldn't fit, and never blocking a plan when the check itself fails."""

from botocore.exceptions import ClientError

from deploy import preflight

CREDS = {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "s", "AWS_SESSION_TOKEN": "t"}


class _Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self):
        return iter(self.pages)


class _Session:
    """Records every API call so the test can prove the check is read-only."""

    def __init__(self, vpcs=0, quota=None, describe_error=False):
        self.vpcs, self.quota, self.describe_error = vpcs, quota, describe_error
        self.calls = []

    def client(self, name, config=None):
        session = self

        class _Client:
            def get_paginator(self, op):
                session.calls.append(op)
                if session.describe_error:
                    raise ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": "no"}}, "DescribeVpcs")
                return _Paginator([{"Vpcs": [{}] * min(session.vpcs, 3)}, {"Vpcs": [{}] * max(session.vpcs - 3, 0)}])

            def get_service_quota(self, ServiceCode, QuotaCode):
                session.calls.append("get_service_quota")
                if session.quota is None:
                    raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetServiceQuota")
                return {"Quota": {"Value": float(session.quota)}}

        return _Client()


def _check(monkeypatch, session):
    monkeypatch.setattr(preflight, "_session", lambda creds, region: session)
    return preflight.vpc_quota_problem(CREDS, "us-east-1")


def test_room_for_another_vpc(monkeypatch):
    assert _check(monkeypatch, _Session(vpcs=4, quota=5)) is None


def test_full_region_is_reported_before_anything_is_created(monkeypatch):
    message = _check(monkeypatch, _Session(vpcs=5, quota=5))
    assert "already has 5 VPCs in us-east-1" in message and "Nothing was created" in message


def test_a_raised_quota_is_respected(monkeypatch):
    assert _check(monkeypatch, _Session(vpcs=7, quota=20)) is None


def test_without_quota_access_the_aws_default_applies(monkeypatch):
    assert _check(monkeypatch, _Session(vpcs=5, quota=None)) is not None
    assert _check(monkeypatch, _Session(vpcs=2, quota=None)) is None


def test_a_failed_check_never_blocks_the_plan(monkeypatch):
    assert _check(monkeypatch, _Session(describe_error=True)) is None


def test_only_read_operations_are_called(monkeypatch):
    session = _Session(vpcs=5, quota=5)
    _check(monkeypatch, session)
    assert session.calls == ["describe_vpcs", "get_service_quota"]
