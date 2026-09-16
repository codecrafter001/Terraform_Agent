"""Abstract cloud discovery interface.

AWSScanner (aws_scanner.py) is today's only real implementation. This
interface exists so a future Azure/GCP discovery module can be dropped in
without cloud_discovery_node (the LangGraph agent that calls it) needing to
know which cloud it's talking to - it only depends on this contract.
AzureDiscovery/GCPDiscovery below are unimplemented stubs: real
implementations need their own read-only-only credential model and API
surface (Azure Resource Manager / GCP Resource Manager), which is out of
scope for this pass and left as a clearly-marked follow-up rather than
guessed at.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class CloudDiscoveryInterface(ABC):
    """Every provider implementation must expose read-only discovery only -
    the same hard safety rule (Describe*/Get*/List* APIs only, never a
    mutating call) applies regardless of which cloud is being scanned."""

    @abstractmethod
    def scan_all(self, filters: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Returns a flat list of discovered resources, each a dict with at
        least resource_type, id, and name keys - the shape terraform_composer
        and the rest of the pipeline already consume."""
        raise NotImplementedError


class AzureDiscovery(CloudDiscoveryInterface):
    """Stub - not yet implemented. Would use azure-mgmt-resource /
    azure-identity with a read-only-scoped service principal."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "Azure discovery is not implemented yet. AWSScanner (tools/aws_scanner.py) "
            "is the only working CloudDiscoveryInterface implementation."
        )

    def scan_all(self, filters: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError


class GCPDiscovery(CloudDiscoveryInterface):
    """Stub - not yet implemented. Would use google-cloud-resource-manager /
    a read-only-scoped service account."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "GCP discovery is not implemented yet. AWSScanner (tools/aws_scanner.py) "
            "is the only working CloudDiscoveryInterface implementation."
        )

    def scan_all(self, filters: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError
