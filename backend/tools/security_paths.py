"""Where the repo's security/ directory (OPA policies, Checkov config) lives: /app/security
in the containers (docker-compose mounts it there), <repo>/security on a host run."""

import os

_CANDIDATES = (
    "/app/security",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "security"),
)


def security_path(*parts: str) -> str:
    """The first candidate that exists, else the container path (so a missing install
    still fails loudly with the familiar path instead of a guessed one)."""
    for root in _CANDIDATES:
        path = os.path.join(root, *parts)
        if os.path.exists(path):
            return path
    return os.path.join(_CANDIDATES[0], *parts)
