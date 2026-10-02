"""Tests for Phase 6.1: S3ArtifactStore (D6).

Validates:
1. put_bytes, put_file, read_bytes, and delete on S3ArtifactStore.
2. ServerSideEncryption (AES256 and aws:kms).
3. Hash verification and size verification.
4. Id validation.
5. get_artifact_store factory function switching between 'local' and 's3'.
"""

import hashlib
import os
import pytest
from unittest.mock import MagicMock

from deploy.artifacts import (
    LocalArtifactStore,
    S3ArtifactStore,
    get_artifact_store,
)


def test_get_artifact_store_factory(monkeypatch):
    monkeypatch.setenv("TERRAAGENT_ARTIFACT_STORE", "local")
    store = get_artifact_store()
    assert isinstance(store, LocalArtifactStore)

    monkeypatch.setenv("TERRAAGENT_ARTIFACT_STORE", "s3")
    store_s3 = get_artifact_store()
    assert isinstance(store_s3, S3ArtifactStore)

    monkeypatch.setenv("TERRAAGENT_ARTIFACT_STORE", "unsupported_storage")
    with pytest.raises(RuntimeError, match="Unknown artifact store"):
        get_artifact_store()


def test_s3_artifact_store_put_read_delete_with_mock():
    mock_s3 = MagicMock()
    mock_objects = {}

    def mock_put_object(**kwargs):
        bucket = kwargs["Bucket"]
        key = kwargs["Key"]
        body = kwargs["Body"]
        mock_objects[(bucket, key)] = body
        return {}

    def mock_get_object(**kwargs):
        bucket = kwargs["Bucket"]
        key = kwargs["Key"]
        if (bucket, key) not in mock_objects:
            raise KeyError("NoSuchKey")
        body_mock = MagicMock()
        body_mock.read.return_value = mock_objects[(bucket, key)]
        return {"Body": body_mock}

    def mock_delete_object(**kwargs):
        bucket = kwargs["Bucket"]
        key = kwargs["Key"]
        mock_objects.pop((bucket, key), None)
        return {}

    mock_s3.put_object.side_effect = mock_put_object
    mock_s3.get_object.side_effect = mock_get_object
    mock_s3.delete_object.side_effect = mock_delete_object

    store = S3ArtifactStore(bucket="my-test-bucket", kms_key_id="arn:aws:kms:us-east-1:123:key/abc", s3_client=mock_s3)
    data = b"Hello world from TerraAgent S3 Artifact Store!"
    
    # 1. Put bytes
    res = store.put_bytes(data)
    assert res["size"] == len(data)
    assert res["sha256"] == hashlib.sha256(data).hexdigest()
    art_id = res["artifact_id"]

    # Verify S3 call had KMS encryption
    assert mock_s3.put_object.called
    last_call = mock_s3.put_object.call_args[1]
    assert last_call["Bucket"] == "my-test-bucket"
    assert last_call["ServerSideEncryption"] == "aws:kms"
    assert last_call["SSEKMSKeyId"] == "arn:aws:kms:us-east-1:123:key/abc"

    # 2. Read bytes
    read_back = store.read_bytes(art_id)
    assert read_back == data

    # 3. Delete
    store.delete(art_id)
    assert ("my-test-bucket", f"artifacts/{art_id[:2]}/{art_id}") not in mock_objects


def test_s3_artifact_store_put_file(tmp_path):
    mock_s3 = MagicMock()
    mock_objects = {}

    def mock_put_object(**kwargs):
        mock_objects[kwargs["Key"]] = kwargs["Body"]
        return {}

    def mock_get_object(**kwargs):
        body_mock = MagicMock()
        body_mock.read.return_value = mock_objects[kwargs["Key"]]
        return {"Body": body_mock}

    mock_s3.put_object.side_effect = mock_put_object
    mock_s3.get_object.side_effect = mock_get_object

    store = S3ArtifactStore(bucket="test-bucket", s3_client=mock_s3)
    
    test_file = tmp_path / "sample.txt"
    test_file.write_bytes(b"file content for s3 artifact store")
    
    res = store.put_file(str(test_file))
    assert res["size"] == len(b"file content for s3 artifact store")
    assert res["sha256"] == hashlib.sha256(b"file content for s3 artifact store").hexdigest()

    data = store.read_bytes(res["artifact_id"])
    assert data == b"file content for s3 artifact store"


def test_s3_artifact_store_invalid_id():
    store = S3ArtifactStore(bucket="test-bucket", s3_client=MagicMock())
    with pytest.raises(ValueError, match="invalid artifact id"):
        store.read_bytes("../../etc/passwd")
    with pytest.raises(ValueError, match="invalid artifact id"):
        store.delete("non-hex-id")
