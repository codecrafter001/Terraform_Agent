"""Deployment mode source intake: the archive is untrusted, so extraction must
refuse traversal, absolute paths, symlinks, encrypted entries and bombs, and
the GitHub download must only ever follow GitHub's own redirect."""

import asyncio
import io
import os
import zipfile

import httpx
import pytest

import deploy.source_intake as intake
from deploy.source_intake import IntakeError, download_github_archive, extract_archive, normalize_entry_name, validate_ref


def make_zip(entries, symlinks=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
        for name, target in symlinks:
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, target)
    return buf.getvalue()


def test_extracts_files_and_strips_github_top_dir(tmp_path):
    data = make_zip([("acme-site-1a2b3c/index.html", "<h1>hi</h1>"), ("acme-site-1a2b3c/css/app.css", "body{}")])
    src = extract_archive(data, str(tmp_path))
    assert src.paths == ["css/app.css", "index.html"]
    assert src.stripped_prefix == "acme-site-1a2b3c"
    assert (tmp_path / "index.html").read_text() == "<h1>hi</h1>"
    assert all(len(f.sha256) == 64 for f in src.files)


@pytest.mark.parametrize("name", ["../evil.txt", "a/../../evil.txt", "/etc/passwd", "C:/x.txt", "a\\b.txt", "a/\x00b"])
def test_unsafe_entry_names_are_refused(name):
    with pytest.raises(IntakeError):
        normalize_entry_name(name)


# zipfile's own writer rewrites backslashes (on Windows) and truncates at NUL,
# so only these can be put into a real archive portably.
@pytest.mark.parametrize("name", ["../evil.txt", "a/../../evil.txt", "/etc/passwd", "C:/x.txt"])
def test_unsafe_archives_are_refused_before_anything_is_written(name, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(IntakeError):
        extract_archive(make_zip([("ok.txt", "x"), (name, "x")]), str(dest))
    assert not (tmp_path / "evil.txt").exists()
    assert list(dest.iterdir()) == []


def test_dot_segments_are_normalised():
    assert normalize_entry_name("./a//b/./c.txt") == "a/b/c.txt"


def test_symlinks_are_never_extracted(tmp_path):
    src = extract_archive(make_zip([("index.html", "x")], symlinks=[("link", "/etc/passwd")]), str(tmp_path))
    assert src.paths == ["index.html"]
    assert ("link", "symbolic link (never extracted)") in src.dropped
    assert not (tmp_path / "link").exists()


def test_encrypted_entries_are_refused(tmp_path):
    data = bytearray(make_zip([("secret.txt", "x")]))
    # Set the "encrypted" general-purpose flag in both headers.
    for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = data.find(sig)
        data[i + offset] |= 0x1
    with pytest.raises(IntakeError, match="encrypted"):
        extract_archive(bytes(data), str(tmp_path))


def test_dropped_dirs_and_env_files_are_reported_not_extracted(tmp_path):
    src = extract_archive(make_zip([
        ("index.html", "x"), (".env", "SECRET=1"), (".env.production", "x"), ("node_modules/a/index.js", "x"),
        (".git/config", "x"), (".github/workflows/ci.yml", "x"), ("app/__pycache__/m.pyc", "x"),
    ]), str(tmp_path))
    assert src.paths == ["index.html"]
    assert {p for p, _ in src.dropped} == {".env", ".env.production", "node_modules/a/index.js", ".git/config",
                                            ".github/workflows/ci.yml", "app/__pycache__/m.pyc"}
    assert not (tmp_path / ".env").exists()


def test_oversized_file_is_dropped_and_bomb_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "MAX_FILE_BYTES", 1000)
    src = extract_archive(make_zip([("index.html", "x"), ("big.bin", b"\0" * 5000)]), str(tmp_path / "a"))
    assert src.paths == ["index.html"]
    assert not (tmp_path / "a" / "big.bin").exists()

    monkeypatch.setattr(intake, "MAX_FILE_BYTES", 10_000)
    monkeypatch.setattr(intake, "MAX_EXTRACTED_BYTES", 15_000)
    os.makedirs(tmp_path / "b")
    with pytest.raises(IntakeError, match="expands to more than"):
        extract_archive(make_zip([(f"f{i}.bin", b"\0" * 9000) for i in range(3)]), str(tmp_path / "b"))


def test_entry_count_cap_and_duplicates_and_non_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "MAX_ARCHIVE_ENTRIES", 3)
    with pytest.raises(IntakeError, match="entries"):
        extract_archive(make_zip([(f"f{i}", "x") for i in range(4)]), str(tmp_path))
    monkeypatch.setattr(intake, "MAX_ARCHIVE_ENTRIES", 5000)
    with pytest.warns(UserWarning):
        dup = make_zip([("a.txt", "1"), ("a.txt", "2")])
    with pytest.raises(IntakeError, match="more than once"):
        extract_archive(dup, str(tmp_path))
    with pytest.raises(IntakeError, match="not a valid ZIP"):
        extract_archive(b"not a zip", str(tmp_path))
    with pytest.raises(IntakeError, match="no usable files"):
        extract_archive(make_zip([(".env", "x")]), str(tmp_path))


@pytest.mark.parametrize("ref,ok", [("main", True), ("release/1.2", True), ("v1.0.0", True), ("", True),
                                    ("../x", False), ("a b", False), ("/main", False), ("x;rm", False)])
def test_validate_ref(ref, ok):
    if ok:
        validate_ref(ref)
    else:
        with pytest.raises(IntakeError):
            validate_ref(ref)


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_http(monkeypatch, handler):
    real = _REAL_ASYNC_CLIENT

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(intake.httpx, "AsyncClient", factory)


def test_github_download_follows_only_codeload_and_drops_the_token(monkeypatch):
    seen = []
    archive = make_zip([("repo-abc/index.html", "x")])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path, request.headers.get("authorization")))
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": "https://codeload.github.com/acme/site/legacy.zip/refs/heads/main?token=t"})
        return httpx.Response(200, content=archive)

    _patch_http(monkeypatch, handler)
    data = asyncio.run(download_github_archive("acme/site", "main", "ghp_secret"))
    assert data == archive
    assert seen[0] == ("api.github.com", "/repos/acme/site/zipball/main", "Bearer ghp_secret")
    assert seen[1][0] == "codeload.github.com" and seen[1][2] is None


def test_github_download_refuses_unexpected_redirects_and_reports_404(monkeypatch):
    _patch_http(monkeypatch, lambda r: httpx.Response(302, headers={"location": "https://evil.example/x.zip"}))
    with pytest.raises(IntakeError, match="unexpected host"):
        asyncio.run(download_github_archive("acme/site", None, None))

    _patch_http(monkeypatch, lambda r: httpx.Response(404))
    with pytest.raises(IntakeError, match="was not found"):
        asyncio.run(download_github_archive("acme/site", None, None))


def test_github_download_is_size_capped(monkeypatch):
    monkeypatch.setattr(intake, "MAX_UPLOAD_BYTES", 100)
    _patch_http(monkeypatch, lambda r: httpx.Response(200, content=b"x" * 500))
    with pytest.raises(IntakeError, match="larger than"):
        asyncio.run(download_github_archive("acme/site", None, None))
