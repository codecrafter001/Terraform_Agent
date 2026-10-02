"""Tests for Phase 6 Item 2: Short-lived, single-use encrypted credential references

Ensures raw AWS access keys, secret keys, and session tokens never sit in Celery broker arguments
in Redis, and are instead replaced by an encrypted, single-use reference with a TTL that deletes on read.
"""

import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from services.credential_store import (
    _CIPHER,
    _MEMORY_STORE,
    decrypt_payload,
    encrypt_payload,
    retrieve_credential_ref,
    store_credential_ref,
)
import routers.scan as scan_router_module
from services.celery_app import resume_scan_task, run_scan_task


def test_encrypt_decrypt_payload_roundtrip():
    secret_data = {
        "aws_access_key": "AKIAEXAMPLETEST123",
        "aws_secret_key": "secret-key-abcdef-xyz",
        "aws_session_token": "token-12345",
        "zip_password": "custom-password",
    }
    encrypted = encrypt_payload(secret_data)
    assert isinstance(encrypted, str)
    assert "AKIAEXAMPLETEST123" not in encrypted
    assert "secret-key-abcdef-xyz" not in encrypted

    decrypted = decrypt_payload(encrypted)
    assert decrypted == secret_data


def test_store_and_retrieve_single_use_deletion():
    creds = {
        "aws_access_key": "AKIASTORE1",
        "aws_secret_key": "secret-store-1",
    }
    ref = store_credential_ref(creds, ttl_seconds=60)
    assert ref.startswith("cred_ref:")

    # First retrieval should succeed
    retrieved = retrieve_credential_ref(ref, delete=True)
    assert retrieved == creds

    # Second retrieval must return None (single-use deletion)
    second = retrieve_credential_ref(ref, delete=True)
    assert second is None


def test_expired_credential_ref_returns_none(monkeypatch):
    creds = {
        "aws_access_key": "AKIAEXPIRED",
        "aws_secret_key": "secret-expired",
    }
    # Store with 1 second TTL
    ref = store_credential_ref(creds, ttl_seconds=1)

    # Fast-forward in-memory expiration
    if ref in _MEMORY_STORE:
        blob, _ = _MEMORY_STORE[ref]
        _MEMORY_STORE[ref] = (blob, time.time() - 10)

    # Retrieval should be None
    assert retrieve_credential_ref(ref) is None


def test_invalid_or_tampered_token_returns_none():
    assert retrieve_credential_ref("cred_ref:non-existent-token") is None
    assert retrieve_credential_ref("") is None
    assert retrieve_credential_ref(None) is None


def test_scan_endpoint_celery_broker_arguments_contain_no_raw_credentials(monkeypatch):
    from unittest.mock import MagicMock
    from services.redis_client import redis_service

    monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=None))
    monkeypatch.setattr(redis_service, "_redis_available", True)

    mock_scan_task = MagicMock()
    monkeypatch.setattr(scan_router_module, "run_scan_task", mock_scan_task)


    raw_access_key = "AKIAREALTESTKEY999"
    raw_secret_key = "super-secret-aws-key-999"
    raw_session_token = "session-token-999"
    raw_zip_password = "zip-pass-999"

    with TestClient(app) as client:
        payload = {
            "aws_access_key": raw_access_key,
            "aws_secret_key": raw_secret_key,
            "aws_session_token": raw_session_token,
            "zip_password": raw_zip_password,
            "region": "us-east-1",
            "operation": "generate",
            "resource_filters": ["VPC"],
        }
        resp = client.post("/api/scan", json=payload)
        assert resp.status_code == 202

    mock_scan_task.apply_async.assert_called_once()
    _, call_kwargs = mock_scan_task.apply_async.call_args
    dispatched_args = call_kwargs.get("args")
    assert dispatched_args is not None
    job_id, dispatched_dict = dispatched_args

    assert job_id.startswith("job-")

    # Assert raw secrets are NOT in the Celery broker payload dictionary
    assert "aws_access_key" not in dispatched_dict or dispatched_dict.get("aws_access_key") is None
    assert "aws_secret_key" not in dispatched_dict or dispatched_dict.get("aws_secret_key") is None
    assert "aws_session_token" not in dispatched_dict or dispatched_dict.get("aws_session_token") is None
    assert "zip_password" not in dispatched_dict or dispatched_dict.get("zip_password") is None

    # Assert raw secrets never appear as substrings in the dispatched broker arguments
    serialized_broker_message = str(dispatched_args)
    assert raw_access_key not in serialized_broker_message
    assert raw_secret_key not in serialized_broker_message
    assert raw_session_token not in serialized_broker_message
    assert raw_zip_password not in serialized_broker_message


    # Assert credential_ref is present
    cred_ref = dispatched_dict["credential_ref"]
    assert cred_ref.startswith("cred_ref:")

    # Verify run_scan_task resolves credentials from cred_ref
    with patch("services.pipeline.run_pipeline", new_callable=AsyncMock) as mock_pipeline:
        run_scan_task(job_id, dispatched_dict)
        mock_pipeline.assert_called_once()
        passed_job_id, passed_dict = mock_pipeline.call_args[0]
        assert passed_job_id == job_id
        assert passed_dict["aws_access_key"] == raw_access_key
        assert passed_dict["aws_secret_key"] == raw_secret_key
        assert passed_dict["aws_session_token"] == raw_session_token
        assert passed_dict["zip_password"] == raw_zip_password

    # Second execution has no credentials remaining in the store
    assert retrieve_credential_ref(cred_ref) is None



def test_resume_scan_task_resolves_credential_ref():
    creds = {
        "aws_access_key": "AKIARESUME123",
        "aws_secret_key": "secret-resume-123",
    }
    ref = store_credential_ref(creds, ttl_seconds=60)

    with patch("services.pipeline.resume_pipeline", new_callable=AsyncMock) as mock_resume:
        resume_scan_task("job-test-resume", {"approved": True}, {"credential_ref": ref})
        mock_resume.assert_called_once()
        called_job_id, called_decision, called_creds = mock_resume.call_args[0]
        assert called_job_id == "job-test-resume"
        assert called_decision == {"approved": True}
        assert called_creds == creds

    # Credential reference is consumed / deleted
    assert retrieve_credential_ref(ref) is None
