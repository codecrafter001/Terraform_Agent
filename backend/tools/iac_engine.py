"""IaC Engine Abstraction: Unified interface for Terraform and OpenTofu execution.

Provides an extensible engine hierarchy that enforces centralized safety boundaries
and guarantees drop-in parity between HashiCorp Terraform and Linux Foundation OpenTofu.
"""

from abc import ABC, abstractmethod
import asyncio
import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

from tools.sandbox_registry import create_sandbox, release_sandbox
from tools.terraform_runner import TerraformRunner

logger = logging.getLogger("terraagent.iac_engine")

SUPPORTED_ENGINES = {"terraform", "tofu", "opentofu"}


class IaCEngine(ABC):
    """Abstract base class for Infrastructure-as-Code CLI engines."""

    def __init__(self, binary_path: Optional[str] = None):
        self._custom_binary_path = binary_path

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable engine identifier (e.g. 'terraform' or 'opentofu')."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """User-facing engine display title (e.g. 'Terraform' or 'OpenTofu')."""
        pass

    @property
    @abstractmethod
    def default_binary_name(self) -> str:
        """Default CLI executable name ('terraform' or 'tofu')."""
        pass

    @property
    def binary_name(self) -> str:
        """CLI executable path or name."""
        return self._custom_binary_path or self.default_binary_name

    def build_fmt_command(self) -> List[str]:
        return [self.binary_name, "fmt", "-check", "-diff"]

    def build_init_command(self, backend: bool = False) -> List[str]:
        cmd = [self.binary_name, "init"]
        if not backend:
            cmd.append("-backend=false")
        return cmd

    def build_validate_command(self) -> List[str]:
        return [self.binary_name, "validate", "-json"]

    async def run_command(self, args: List[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
        """Executes a command through the centralized TerraformRunner safety chokepoint."""
        cmd = [self.binary_name] + args if not args or args[0] != self.binary_name else args
        return await TerraformRunner.run_command(cmd, cwd=cwd)

    async def get_version(self) -> str:
        """Fetches the CLI version string safely without logging credentials."""
        try:
            code, out, err = await self.run_command(["version"], cwd=os.getcwd())
            if code == 0 and out:
                first_line = out.strip().split("\n")[0]
                return first_line
        except Exception as e:
            logger.warning(f"Could not determine {self.name} version: {e}")
        return f"{self.display_name} (version unknown)"

    async def format_hcl(self, hcl_files: Dict[str, str]) -> Dict[str, str]:
        """Canonical HCL formatting using `<binary> fmt` in a sandbox."""
        return await TerraformRunner.format_hcl(hcl_files, binary=self.binary_name)

    async def validate_hcl(self, hcl_files: Dict[str, str]) -> Dict[str, Any]:
        """Validates HCL files in a sandbox via fmt -check, init -backend=false, and validate."""
        return await TerraformRunner.validate_hcl(hcl_files, binary=self.binary_name)


class TerraformEngine(IaCEngine):
    """Engine implementation for HashiCorp Terraform."""

    @property
    def name(self) -> str:
        return "terraform"

    @property
    def display_name(self) -> str:
        return "Terraform"

    @property
    def default_binary_name(self) -> str:
        return "terraform"


class OpenTofuEngine(IaCEngine):
    """Engine implementation for Linux Foundation OpenTofu."""

    @property
    def name(self) -> str:
        return "opentofu"

    @property
    def display_name(self) -> str:
        return "OpenTofu"

    @property
    def default_binary_name(self) -> str:
        return "tofu"


def get_iac_engine(engine_name: Optional[str] = "terraform") -> IaCEngine:
    """Factory to retrieve the configured IaCEngine instance.

    Validates engine choice against the supported whitelist to prevent
    arbitrary binary execution or path traversal.
    """
    normalized = (engine_name or "terraform").strip().lower()

    if normalized not in SUPPORTED_ENGINES:
        raise ValueError(
            f"Unsupported IaC engine: '{engine_name}'. Allowed values are: 'terraform', 'tofu', 'opentofu'."
        )

    if normalized in ("tofu", "opentofu"):
        return OpenTofuEngine()
    return TerraformEngine()
