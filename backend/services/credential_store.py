"""Short-lived, single-use encrypted credential store for Celery/Redis tasks.

Replaces raw AWS credentials in Celery broker arguments with an opaque,
encrypted reference (cred_ref:...). The encrypted payload has a short TTL (default 10m)
and is deleted immediately upon the first read (single-use).
"""

import base64
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet
import redis

logger = logging.getLogger(__name__)

# Secret key used for symmetric encryption of short-lived credentials in Redis
_RAW_SECRET = (
    os.getenv("TERRAAGENT_SECRET_KEY")
    or os.getenv("SECRET_KEY")
    or "terraagent-default-broker-encryption-secret-key-32b"
)
_FERNET_KEY = base64.urlsafe_b64encode(hashlib.sha256(_RAW_SECRET.encode()).digest())
_CIPHER = Fernet(_FERNET_KEY)

# In-memory fallback for environments without active Redis
_MEMORY_STORE: Dict[str, tuple[str, float]] = {}


def _get_sync_redis_client() -> Optional[redis.Redis]:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        client.ping()
        return client
    except Exception:
        return None


def encrypt_payload(data: Dict[str, Any]) -> str:
    """Encrypt a dictionary into a URL-safe Fernet ciphertext string."""
    raw_bytes = json.dumps(data).encode("utf-8")
    return _CIPHER.encrypt(raw_bytes).decode("utf-8")


def decrypt_payload(ciphertext: str) -> Optional[Dict[str, Any]]:
    """Decrypt a Fernet ciphertext string into a dictionary."""
    try:
        decrypted_bytes = _CIPHER.decrypt(ciphertext.encode("utf-8"))
        return json.loads(decrypted_bytes.decode("utf-8"))
    except Exception as e:
        logger.warning(f"Failed to decrypt credential payload: {e}")
        return None


def store_credential_ref(credentials: Dict[str, Any], ttl_seconds: int = 600) -> str:
    """Encrypts credentials and stores them under a single-use token in Redis with TTL.

    Returns the opaque token string 'cred_ref:<uuid>'.
    """
    ref_id = f"cred_ref:{uuid.uuid4().hex}"
    encrypted_blob = encrypt_payload(credentials)
    expires_at = time.time() + ttl_seconds

    # Always keep in memory fallback
    _MEMORY_STORE[ref_id] = (encrypted_blob, expires_at)

    # Prune expired items from memory store
    now = time.time()
    expired_keys = [k for k, (_, exp) in _MEMORY_STORE.items() if exp < now]
    for k in expired_keys:
        _MEMORY_STORE.pop(k, None)

    # Store in Redis if reachable
    client = _get_sync_redis_client()
    if client:
        try:
            client.set(ref_id, encrypted_blob, ex=ttl_seconds)
        except Exception as e:
            logger.debug(f"Redis store_credential_ref fallback to memory: {e}")

    return ref_id


def retrieve_credential_ref(cred_ref: str, delete: bool = True) -> Optional[Dict[str, Any]]:
    """Retrieves and decrypts credentials stored under cred_ref.

    If delete=True (default), deletes the key immediately on read (single-use).
    """
    if not cred_ref or not isinstance(cred_ref, str):
        return None

    encrypted_blob: Optional[str] = None
    client = _get_sync_redis_client()

    if client:
        try:
            if delete:
                # Atomically get and delete or pipeline
                pipe = client.pipeline()
                pipe.get(cred_ref)
                pipe.delete(cred_ref)
                results = pipe.execute()
                encrypted_blob = results[0]
            else:
                encrypted_blob = client.get(cred_ref)
        except Exception as e:
            logger.debug(f"Redis retrieve_credential_ref fallback to memory: {e}")

    # Fallback to memory store if not found in Redis
    if not encrypted_blob and cred_ref in _MEMORY_STORE:
        blob, exp = _MEMORY_STORE.get(cred_ref, (None, 0))
        if exp >= time.time():
            encrypted_blob = blob
        if delete:
            _MEMORY_STORE.pop(cred_ref, None)

    if not encrypted_blob:
        return None

    return decrypt_payload(encrypted_blob)
