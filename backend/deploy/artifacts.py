"""Artifact storage for deployment mode (design doc §12).

`LocalArtifactStore` writes under ARTIFACT_ROOT on the volume shared by the API
and the workers. Each artifact gets a random id (never derived from content, so
deleting one deployment's copy can't break another's), is written atomically,
and is readable by the service user only. The S3 + KMS store is Phase 6; the
interface is what callers depend on.
"""

import hashlib
import os
import re
import uuid
from typing import Any, Optional, Protocol, TypedDict

from deploy.config import ARTIFACT_ROOT

_ARTIFACT_ID = re.compile(r"^[0-9a-f]{32}$")


class StoredArtifact(TypedDict):
    artifact_id: str
    sha256: str
    size: int


class ArtifactStore(Protocol):
    def put_bytes(self, data: bytes) -> StoredArtifact: ...
    def put_file(self, path: str) -> StoredArtifact: ...
    def read_bytes(self, artifact_id: str) -> bytes: ...
    def delete(self, artifact_id: str) -> None: ...


class LocalArtifactStore:
    def __init__(self, root: str = ARTIFACT_ROOT):
        self.root = root

    def _path(self, artifact_id: str) -> str:
        # Ids are only ever ones this store generated; anything else could be
        # a path, so it is refused rather than joined.
        if not _ARTIFACT_ID.match(artifact_id):
            raise ValueError("invalid artifact id")
        return os.path.join(self.root, artifact_id[:2], artifact_id)

    def _write(self, artifact_id: str, chunks) -> StoredArtifact:
        path = self._path(artifact_id)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        digest = hashlib.sha256()
        size = 0
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in chunks:
                    digest.update(chunk)
                    size += len(chunk)
                    f.write(chunk)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return {"artifact_id": artifact_id, "sha256": digest.hexdigest(), "size": size}

    def put_bytes(self, data: bytes) -> StoredArtifact:
        return self._write(uuid.uuid4().hex, [data])

    def put_file(self, path: str) -> StoredArtifact:
        def chunks():
            with open(path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        return self._write(uuid.uuid4().hex, chunks())

    def read_bytes(self, artifact_id: str) -> bytes:
        with open(self._path(artifact_id), "rb") as f:
            return f.read()

    def delete(self, artifact_id: str) -> None:
        try:
            os.remove(self._path(artifact_id))
        except FileNotFoundError:
            pass


class S3ArtifactStore:
    """Artifact storage backed by an S3 bucket with server-side encryption (SSE-KMS or AES256).
    Each artifact gets a cryptographically unique 32-hex ID, matching the ArtifactStore protocol.
    """

    def __init__(
        self,
        bucket: Optional[str] = None,
        prefix: str = "artifacts",
        kms_key_id: Optional[str] = None,
        region: Optional[str] = None,
        s3_client: Any = None,
    ):
        self.bucket = bucket or os.getenv("TERRAAGENT_ARTIFACT_BUCKET", "terraagent-artifacts")
        self.prefix = prefix.strip("/")
        self.kms_key_id = kms_key_id or os.getenv("TERRAAGENT_ARTIFACT_KMS_KEY")
        self.region = region or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        self._client = s3_client

    def _get_client(self):
        if self._client is not None:
            return self._client
        import boto3
        self._client = boto3.client("s3", region_name=self.region)
        return self._client

    def _key(self, artifact_id: str) -> str:
        if not _ARTIFACT_ID.match(artifact_id):
            raise ValueError("invalid artifact id")
        return f"{self.prefix}/{artifact_id[:2]}/{artifact_id}"

    def put_bytes(self, data: bytes) -> StoredArtifact:
        artifact_id = uuid.uuid4().hex
        digest = hashlib.sha256(data).hexdigest()
        size = len(data)
        key = self._key(artifact_id)

        put_kwargs = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": data,
            "Metadata": {"sha256": digest, "size": str(size)},
        }
        if self.kms_key_id:
            put_kwargs["ServerSideEncryption"] = "aws:kms"
            put_kwargs["SSEKMSKeyId"] = self.kms_key_id
        else:
            put_kwargs["ServerSideEncryption"] = "AES256"

        client = self._get_client()
        client.put_object(**put_kwargs)
        return {"artifact_id": artifact_id, "sha256": digest, "size": size}

    def put_file(self, path: str) -> StoredArtifact:
        with open(path, "rb") as f:
            data = f.read()
        return self.put_bytes(data)

    def read_bytes(self, artifact_id: str) -> bytes:
        key = self._key(artifact_id)
        client = self._get_client()
        response = client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def delete(self, artifact_id: str) -> None:
        key = self._key(artifact_id)
        client = self._get_client()
        try:
            client.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            pass


def get_artifact_store() -> ArtifactStore:
    kind = os.getenv("TERRAAGENT_ARTIFACT_STORE", "local").lower()
    if kind == "local":
        return LocalArtifactStore()
    if kind == "s3":
        return S3ArtifactStore()
    raise RuntimeError(f"Unknown artifact store '{kind}' (supported: 'local', 's3')")
