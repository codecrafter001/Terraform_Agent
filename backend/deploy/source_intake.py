"""Safe intake of an uploaded ZIP or a GitHub repository archive (design doc §9.1).

The archive is untrusted. Every entry name is normalised and checked before
anything touches disk (no absolute paths, drive letters, `..`, backslashes or
NULs), symlinks and encrypted entries are never extracted, and reads are
streamed with hard caps on entry count, per-file size and total size - zip
headers are not trusted for sizes, so a zip bomb stops at the cap.

Nothing extracted is ever executed or imported here.
"""

import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx

from deploy.config import (
    GITHUB_DOWNLOAD_TIMEOUT_SECONDS,
    MAX_ARCHIVE_ENTRIES,
    MAX_EXTRACTED_BYTES,
    MAX_FILE_BYTES,
    MAX_PATH_LENGTH,
    MAX_UPLOAD_BYTES,
)

# Never extracted: VCS/CI metadata, local tool state, dependency and cache
# directories (rebuilt from manifests, never trusted from the upload), and
# environment files (almost always secrets).
DROPPED_DIRS = frozenset({
    ".git", ".github", ".terraform", "terraform.d", "node_modules", "__pycache__", ".venv", "venv",
    ".pytest_cache", ".mypy_cache",
})
DROPPED_FILES = frozenset({".DS_Store", "Thumbs.db"})
_ENV_FILE = re.compile(r"^\.env(\..*)?$")
_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_ZIP_SYMLINK = 0o120000


class IntakeError(ValueError):
    """The source can't be accepted. The message is safe to show the user."""


@dataclass
class SourceFile:
    path: str
    size: int
    sha256: str


@dataclass
class ExtractedSource:
    root: str
    files: List[SourceFile] = field(default_factory=list)
    dropped: List[Tuple[str, str]] = field(default_factory=list)  # (path, reason)
    stripped_prefix: Optional[str] = None

    @property
    def paths(self) -> List[str]:
        return [f.path for f in self.files]

    def summary(self, limit: int = 200) -> dict:
        return {
            "file_count": len(self.files),
            "total_bytes": sum(f.size for f in self.files),
            "dropped_count": len(self.dropped),
            "dropped": [{"path": p, "reason": r} for p, r in self.dropped[:limit]],
            "stripped_prefix": self.stripped_prefix,
        }


def normalize_entry_name(name: str) -> str:
    """The POSIX relative path an archive entry may be written to, or IntakeError."""
    if not name or "\x00" in name:
        raise IntakeError("the archive contains an entry with an empty or invalid name")
    if "\\" in name or ":" in name:
        raise IntakeError(f"unsupported path in archive: {name[:120]!r} (backslashes and ':' are not allowed)")
    if name.startswith("/"):
        raise IntakeError(f"absolute path in archive: {name[:120]!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        raise IntakeError("the archive contains an entry with an empty name")
    if any(p == ".." for p in parts):
        raise IntakeError(f"path traversal in archive: {name[:120]!r}")
    path = "/".join(parts)
    if len(path) > MAX_PATH_LENGTH:
        raise IntakeError(f"path too long in archive ({len(path)} characters, max {MAX_PATH_LENGTH})")
    return path


def drop_reason(path: str) -> Optional[str]:
    parts = path.split("/")
    for part in parts[:-1]:
        if part in DROPPED_DIRS:
            return f"inside {part}/ (never packaged)"
    name = parts[-1]
    if name in DROPPED_FILES:
        return "OS metadata file"
    if _ENV_FILE.match(name):
        return "environment file (may hold secrets; never packaged)"
    if name.endswith(".pyc"):
        return "compiled Python file"
    return None


def _common_prefix(paths: List[str]) -> Optional[str]:
    """The single top-level directory every entry sits under (GitHub archives
    wrap the repo in `owner-repo-sha/`), or None."""
    firsts = {p.split("/", 1)[0] for p in paths}
    if len(firsts) == 1 and all("/" in p for p in paths):
        return firsts.pop()
    return None


def is_zip(data: bytes) -> bool:
    return zipfile.is_zipfile(io.BytesIO(data))


def extract_archive(data: bytes, dest: str) -> ExtractedSource:
    """Extract `data` (a ZIP) into the existing directory `dest`."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise IntakeError("the upload is not a valid ZIP archive")

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise IntakeError(f"the archive has {len(infos)} entries (max {MAX_ARCHIVE_ENTRIES})")

        entries: List[Tuple[str, zipfile.ZipInfo]] = []
        result = ExtractedSource(root=dest)
        seen = set()
        for info in infos:
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise IntakeError("the archive contains encrypted entries; upload an unencrypted ZIP")
            path = normalize_entry_name(info.filename)
            if path in seen:
                raise IntakeError(f"the archive contains '{path}' more than once")
            seen.add(path)
            if (info.external_attr >> 16) & 0o170000 == _ZIP_SYMLINK:
                result.dropped.append((path, "symbolic link (never extracted)"))
                continue
            entries.append((path, info))

        prefix = _common_prefix([p for p, _ in entries]) if entries else None
        result.stripped_prefix = prefix
        if prefix:
            entries = [(p[len(prefix) + 1:], i) for p, i in entries]
            result.dropped = [(p[len(prefix) + 1:] if p.startswith(prefix + "/") else p, r) for p, r in result.dropped]

        total = 0
        root_real = os.path.realpath(dest)
        for path, info in entries:
            reason = drop_reason(path)
            if reason:
                result.dropped.append((path, reason))
                continue
            target = os.path.realpath(os.path.join(dest, *path.split("/")))
            if not target.startswith(root_real + os.sep):
                raise IntakeError(f"path escapes the archive root: {path[:120]!r}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            too_large = False
            with zf.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(64 * 1024):
                    if size + len(chunk) > MAX_FILE_BYTES:
                        too_large = True
                        break
                    size += len(chunk)
                    total += len(chunk)
                    if total > MAX_EXTRACTED_BYTES:
                        raise IntakeError(f"the archive expands to more than {MAX_EXTRACTED_BYTES // (1024 * 1024)} MB")
                    digest.update(chunk)
                    out.write(chunk)
            if too_large:
                os.remove(target)
                total -= size
                result.dropped.append((path, f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"))
                continue
            result.files.append(SourceFile(path=path, size=size, sha256=digest.hexdigest()))

    if not result.files:
        raise IntakeError("the archive contains no usable files")
    result.files.sort(key=lambda f: f.path)
    return result


def validate_ref(ref: Optional[str]) -> Optional[str]:
    ref = (ref or "").strip()
    if not ref:
        return None
    if not _REF.match(ref) or ".." in ref or ref.startswith("/") or ref.endswith("/"):
        raise IntakeError("ref must be a branch, tag or commit SHA")
    return ref


async def _read_capped(resp: httpx.Response) -> bytes:
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf.extend(chunk)
        if len(buf) > MAX_UPLOAD_BYTES:
            raise IntakeError(f"the repository archive is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    return bytes(buf)


async def download_github_archive(repo: str, ref: Optional[str], token: Optional[str]) -> bytes:
    """The repository's ZIP archive through the GitHub API (`/zipball`).

    `repo` must already be normalised (tools/github_repo.normalize_repo). The
    token, if any, is used for this call only and never stored or logged. The
    API answers with a redirect to codeload.github.com carrying a short-lived
    URL; that hop is followed only to that host, without the Authorization
    header."""
    from services.github_client import GITHUB_API_BASE, _handle_api_error

    ref = validate_ref(ref)
    path = f"/repos/{repo}/zipball" + (f"/{quote(ref, safe='/')}" if ref else "")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(base_url=GITHUB_API_BASE, headers=headers,
                                 timeout=GITHUB_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
        async with client.stream("GET", path) as resp:
            if resp.status_code == 200:
                return await _read_capped(resp)
            if resp.status_code == 404:
                raise IntakeError(
                    f"'{repo}'{f' at {ref}' if ref else ''} was not found. Check the name and ref; "
                    "a private repository needs a token with Contents: Read-only access."
                )
            if resp.status_code not in (301, 302, 303, 307, 308):
                await resp.aread()
                raise IntakeError(str(_handle_api_error(resp, f"downloading '{repo}'")))
            location = resp.headers.get("location", "")

    parsed = urlparse(location)
    if parsed.scheme != "https" or parsed.hostname != "codeload.github.com":
        raise IntakeError("GitHub redirected the download to an unexpected host; refusing to follow it")
    async with httpx.AsyncClient(timeout=GITHUB_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
        async with client.stream("GET", location) as resp:
            if resp.status_code != 200:
                raise IntakeError(f"downloading the archive from GitHub failed ({resp.status_code})")
            return await _read_capped(resp)
