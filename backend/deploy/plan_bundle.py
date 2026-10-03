"""Plan bundle packaging and verification (design doc §3.5).

Packages the exact working directory (rendered files, artifacts, lockfile, tfplan)
into a deterministic archive and computes its SHA256 hash (plan_bundle_sha256).
Approval binds to this hash, ensuring that apply in Phase 4 executes the exact
approved plan without drift.
"""

import hashlib
import io
import os
import zipfile
from dataclasses import dataclass
from typing import Tuple

# Directories to exclude from the bundle
_EXCLUDED_DIRS = {".terraform", ".git", "__pycache__", ".pytest_cache"}
_EXCLUDED_EXTS = {".tmp"}


def create_plan_bundle(workdir: str) -> Tuple[bytes, str]:
    """Creates a deterministic ZIP of workdir (excluding .terraform/) and returns
    (bundle_bytes, sha256_hex).
    """
    buf = io.BytesIO()
    # Fixed timestamp (2020-01-01 00:00:00) and permissions for reproducibility
    fixed_time = (2020, 1, 1, 0, 0, 0)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(workdir):
            # Sort for deterministic entry ordering
            dirs.sort()
            dirs[:] = [d for d in dirs if d not in _EXCLUDED_DIRS]

            for fname in sorted(files):
                if any(fname.endswith(ext) for ext in _EXCLUDED_EXTS):
                    continue
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, workdir).replace(os.sep, "/")

                with open(full_path, "rb") as f:
                    data = f.read()

                zinfo = zipfile.ZipInfo(rel_path, date_time=fixed_time)
                zinfo.compress_type = zipfile.ZIP_DEFLATED
                zinfo.external_attr = 0o644 << 16  # standard read/write permissions
                zf.writestr(zinfo, data)

    bundle_bytes = buf.getvalue()
    bundle_sha256 = hashlib.sha256(bundle_bytes).hexdigest()
    return bundle_bytes, bundle_sha256


def verify_plan_bundle(bundle_bytes: bytes, expected_sha256: str) -> bool:
    """Verifies that the bundle bytes match the expected SHA-256 hash."""
    if not bundle_bytes or not expected_sha256:
        return False
    actual_sha256 = hashlib.sha256(bundle_bytes).hexdigest()
    return actual_sha256.lower() == expected_sha256.lower()


@dataclass(frozen=True)
class ExtractedBundle:
    path: str
    sha256: str


def extract_plan_bundle(bundle_bytes: bytes, dest_dir: str) -> ExtractedBundle:
    """Extracts a plan bundle safely into dest_dir and returns where it went
    plus the SHA-256 of the bytes that were extracted (what approval binds to)."""
    root = os.path.abspath(dest_dir)
    with zipfile.ZipFile(io.BytesIO(bundle_bytes), "r") as zf:
        for member in zf.infolist():
            # Prevent zip-slip (commonpath, so /tmp/a can't escape into /tmp/ab)
            target_path = os.path.abspath(os.path.join(root, member.filename))
            if os.path.commonpath([root, target_path]) != root:
                raise ValueError(f"Unsafe path in bundle: {member.filename}")
            zf.extract(member, root)
    return ExtractedBundle(path=root, sha256=hashlib.sha256(bundle_bytes).hexdigest())
