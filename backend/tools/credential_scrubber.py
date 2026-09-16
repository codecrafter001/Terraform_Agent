"""Credential Scrubber to ensure AWS secrets and keys are never logged or leaked."""

import re
from typing import Any, Dict

# Regex patterns matching AWS Access Keys, Secret Keys, and Session Tokens
AWS_ACCESS_KEY_REGEX = re.compile(r"(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}")
# {40,} not {40}: AWS secret keys are exactly 40 chars, but session tokens are
# typically 300-400+ chars. An exact {40} match only redacted the first 40
# characters of a longer token, leaving the rest of the real secret exposed in
# "scrubbed" output - verified directly with a realistic-length session token.
AWS_SECRET_KEY_REGEX = re.compile(r"(?i)(aws_secret_access_key|aws_secret_key|secret_key|secret|password|token)([\s:=]+['\"]?)([A-Za-z0-9/+=]{40,})(['\"]?)")
GENERIC_BASE64_KEY_REGEX = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")


class CredentialScrubber:
    """Utility class to scrub credentials from strings, dicts, and log lines."""

    @classmethod
    def scrub_text(cls, text: str) -> str:
        """Scrub known credential patterns from a given string."""
        if not text or not isinstance(text, str):
            return text

        # Redact AWS Access Key IDs
        scrubbed = AWS_ACCESS_KEY_REGEX.sub("[REDACTED_AWS_ACCESS_KEY]", text)

        # Redact AWS Secret Keys matching key-value syntax (keep the key name, redact only the value)
        scrubbed = AWS_SECRET_KEY_REGEX.sub(r"\1\2[REDACTED_SECRET]\4", scrubbed)

        return scrubbed

    @classmethod
    def scrub_dict(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """Deep copy and scrub sensitive keys and text inside dictionaries."""
        scrubbed_data: Dict[str, Any] = {}
        sensitive_keys = {
            "aws_access_key", "aws_secret_key", "aws_session_token",
            "access_key", "secret_key", "session_token", "password",
            "token", "credentials", "aws_credentials", "zip_password",
            "github_token"
        }

        for k, v in data.items():
            if k.lower() in sensitive_keys:
                scrubbed_data[k] = "[REDACTED]"
            elif isinstance(v, dict):
                scrubbed_data[k] = cls.scrub_dict(v)
            elif isinstance(v, list):
                scrubbed_data[k] = [
                    cls.scrub_dict(item) if isinstance(item, dict)
                    else cls.scrub_text(str(item)) if isinstance(item, str)
                    else item
                    for item in v
                ]
            elif isinstance(v, str):
                scrubbed_data[k] = cls.scrub_text(v)
            else:
                scrubbed_data[k] = v

        return scrubbed_data
