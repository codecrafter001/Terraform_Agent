"""Shared pytest fixtures for the TerraAgent backend test suite."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", os.getenv("REDIS_URL", "redis://localhost:6379/0"))

import pytest


@pytest.fixture
def aws_credentials():
    """Fake, non-real AWS credentials - safe to use with moto (which never
    calls real AWS) and never asserted to be real-looking beyond what the
    regex patterns under test require."""
    return {
        "access_key": "AKIAIOSFODNN7EXAMPLE",
        "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "region": "us-east-1",
    }
