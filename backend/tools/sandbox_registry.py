"""Shared sandbox temp-directory tracking with an atexit safety net.

Every tool (terraform/tfsec/checkov/trivy/conftest runners) creates its own
sandbox holding the generated .tf files and removes it in a try/finally block -
that's the primary, reliable cleanup path for the normal exception case. This
adds a second layer: any sandbox still tracked when the process exits (e.g. a
hard kill that skips the finally block entirely) gets swept up here too, so a
sandbox containing discovered-resource configuration never survives past the
process that created it.
"""

import atexit
import logging
import shutil
import tempfile
from typing import Set

logger = logging.getLogger("terraagent.sandbox_registry")

_active_sandboxes: Set[str] = set()


def create_sandbox(prefix: str) -> str:
    """Create a tracked temp directory. Pair with release_sandbox() in a finally block."""
    path = tempfile.mkdtemp(prefix=prefix)
    _active_sandboxes.add(path)
    return path


def release_sandbox(path: str) -> None:
    """Remove a sandbox and stop tracking it. Safe to call even if already removed."""
    shutil.rmtree(path, ignore_errors=True)
    _active_sandboxes.discard(path)


@atexit.register
def _cleanup_all_sandboxes() -> None:
    if not _active_sandboxes:
        return
    remaining = list(_active_sandboxes)
    for path in remaining:
        shutil.rmtree(path, ignore_errors=True)
    _active_sandboxes.clear()
    logger.info(f"atexit cleanup: removed {len(remaining)} orphaned sandbox dir(s)")
