"""Turn whatever a user pastes for a GitHub repository into `owner/repo`.

Accepted: `owner/repo`, `https://github.com/owner/repo` (with or without
`www.`, a trailing slash, `.git`, or extra path like `/tree/main`),
`github.com/owner/repo`, and `git@github.com:owner/repo.git`. Anything else -
another host, a malformed name - is rejected before any GitHub API call, since
the value becomes part of an API path (`/repos/{owner}/{repo}/...`).
"""

import re
from urllib.parse import urlparse

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_HOSTS = {"github.com", "www.github.com"}


def normalize_repo(value: str) -> str:
    raw = (value or "").strip()
    is_url = True
    if not raw:
        raise ValueError("repository is required (owner/repo)")

    if raw.startswith("git@"):
        host, _, path = raw[len("git@"):].partition(":")
        if host.lower() not in _HOSTS:
            raise ValueError("only github.com repositories are supported")
    elif "://" in raw or raw.lower().startswith(("github.com/", "www.github.com/")):
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in _HOSTS:
            raise ValueError("only github.com repositories are supported")
        path = parsed.path
    else:
        path, is_url = raw, False

    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2 or (len(parts) > 2 and not is_url):
        # A bare value must be exactly owner/repo; URLs may carry extra path.
        raise ValueError("repository must look like owner/repo or https://github.com/owner/repo")
    owner, name = parts[0], parts[1]
    if name.lower().endswith(".git"):
        name = name[: -len(".git")]
    if not _OWNER.match(owner) or not _NAME.match(name) or name in (".", ".."):
        raise ValueError("repository must look like owner/repo or https://github.com/owner/repo")
    return f"{owner}/{name}"
