"""Security tests: CredentialScrubber must catch every known AWS credential
shape before it reaches logs, Redis, or SSE output."""

import pytest
from tools.credential_scrubber import CredentialScrubber

FAKE_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
FAKE_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
# Realistic-length STS session token (300+ chars) - the case that exposed the
# {40} vs {40,} regex bug during Phase 7.
FAKE_SESSION_TOKEN = "IQoJb3JpZ2luX2VjEA" + ("A" * 320) + "EXAMPLETOKEN=="


@pytest.mark.parametrize(
    "access_key",
    [
        "AKIAIOSFODNN7EXAMPLE",   # standard access key
        "ASIAV3ZUEFP6EXAMPLE1",   # temporary/STS access key prefix
        "AROAEXAMPLE123456789",   # assumed-role access key prefix
    ],
)
def test_scrub_text_redacts_access_keys(access_key):
    text = f"Using AWS key {access_key} to connect."
    scrubbed = CredentialScrubber.scrub_text(text)
    assert access_key not in scrubbed
    assert "[REDACTED_AWS_ACCESS_KEY]" in scrubbed


def test_scrub_text_redacts_secret_key_value():
    text = f'aws_secret_access_key = "{FAKE_SECRET_KEY}"'
    scrubbed = CredentialScrubber.scrub_text(text)
    assert FAKE_SECRET_KEY not in scrubbed
    assert "[REDACTED_SECRET]" in scrubbed
    # The key name itself is preserved for log readability
    assert "aws_secret_access_key" in scrubbed


def test_scrub_text_redacts_long_session_token_in_full():
    """Regression test for the {40}-vs-{40,} bug: a 300+ char session token
    must be redacted completely, not just its first 40 characters."""
    text = f"session_token: {FAKE_SESSION_TOKEN}"
    scrubbed = CredentialScrubber.scrub_text(text)
    assert FAKE_SESSION_TOKEN not in scrubbed
    # No trailing fragment of the real token should survive
    assert FAKE_SESSION_TOKEN[41:60] not in scrubbed
    assert "[REDACTED_SECRET]" in scrubbed


@pytest.mark.parametrize("key_name", ["secret_key", "password", "token", "secret"])
def test_scrub_text_redacts_generic_secret_key_names(key_name):
    text = f"{key_name}={FAKE_SECRET_KEY}"
    scrubbed = CredentialScrubber.scrub_text(text)
    assert FAKE_SECRET_KEY not in scrubbed


def test_scrub_text_leaves_non_credential_text_untouched():
    text = "The pipeline completed 7 resources in region us-east-1."
    assert CredentialScrubber.scrub_text(text) == text


def test_scrub_text_handles_non_string_input_gracefully():
    assert CredentialScrubber.scrub_text(None) is None
    assert CredentialScrubber.scrub_text(123) == 123


def test_scrub_dict_redacts_sensitive_keys_entirely():
    data = {
        "aws_access_key": FAKE_ACCESS_KEY,
        "aws_secret_key": FAKE_SECRET_KEY,
        "aws_session_token": FAKE_SESSION_TOKEN,
        "region": "us-east-1",
    }
    scrubbed = CredentialScrubber.scrub_dict(data)
    assert scrubbed["aws_access_key"] == "[REDACTED]"
    assert scrubbed["aws_secret_key"] == "[REDACTED]"
    assert scrubbed["aws_session_token"] == "[REDACTED]"
    assert scrubbed["region"] == "us-east-1"


def test_scrub_dict_blanket_redacts_known_credential_container_keys():
    """A dict value under a key literally named e.g. 'aws_credentials' is
    redacted as a single opaque unit rather than recursed into - the safer
    default for a key name that always holds secrets in this codebase."""
    data = {
        "job_id": "job-abc123",
        "aws_credentials": {
            "access_key": FAKE_ACCESS_KEY,
            "secret_key": FAKE_SECRET_KEY,
        },
    }
    scrubbed = CredentialScrubber.scrub_dict(data)
    assert scrubbed["aws_credentials"] == "[REDACTED]"
    assert scrubbed["job_id"] == "job-abc123"


def test_scrub_dict_recurses_into_non_credential_nested_structures():
    data = {
        "job_id": "job-abc123",
        "metadata": {
            "requested_by_key": FAKE_ACCESS_KEY,
        },
        "logs": [f"connecting with {FAKE_ACCESS_KEY}", "no secrets here"],
    }
    scrubbed = CredentialScrubber.scrub_dict(data)
    assert FAKE_ACCESS_KEY not in scrubbed["metadata"]["requested_by_key"]
    assert FAKE_ACCESS_KEY not in scrubbed["logs"][0]
    assert scrubbed["logs"][1] == "no secrets here"
