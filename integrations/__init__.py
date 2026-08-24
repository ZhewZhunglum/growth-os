"""External execution adapters for the Daily Operations runtime.

This package deliberately contains no Django models.  It defines technical
contracts that can later be wired to the versioned business facts owned by the
collection and workflow apps.
"""

from integrations.ai import (
    AIExecutionStatus,
    AIMessage,
    AIRequest,
    AIResult,
    DeepSeekV4Config,
    DeepSeekV4Provider,
    DryRunAIProvider,
    FakeAIProvider,
    StructuredOutputSpec,
)
from integrations.browser_worker import (
    BrowserJobOperation,
    HTTPBrowserWorkerClient,
    HTTPBrowserWorkerConfig,
    BrowserWorkerJob,
    BrowserWorkerPairing,
    BrowserWorkerResult,
)
from integrations.connectors import (
    AcquisitionMode,
    AvailabilityState,
    ConnectorDescriptor,
    ConnectorRequest,
    ConnectorResult,
    ConnectorRunStatus,
    Platform,
    default_connector_catalog,
)
from integrations.connectors.runtime import (
    ConnectorRuntimeConfig,
    JSONAPIRouteConfig,
    UrllibReadOnlyJSONTransport,
    build_connector_registry,
)
from integrations.ingestion import (
    CSVIngestionValidator,
    IngestedEvidence,
    ManualEvidenceInput,
    ProvenancePayload,
    validate_connector_evidence,
    validate_manual_evidence,
)

__all__ = [
    "AIExecutionStatus",
    "AIMessage",
    "AIRequest",
    "AIResult",
    "AcquisitionMode",
    "AvailabilityState",
    "BrowserJobOperation",
    "HTTPBrowserWorkerClient",
    "HTTPBrowserWorkerConfig",
    "BrowserWorkerJob",
    "BrowserWorkerPairing",
    "BrowserWorkerResult",
    "CSVIngestionValidator",
    "ConnectorDescriptor",
    "ConnectorRequest",
    "ConnectorResult",
    "ConnectorRunStatus",
    "ConnectorRuntimeConfig",
    "DeepSeekV4Config",
    "DeepSeekV4Provider",
    "DryRunAIProvider",
    "FakeAIProvider",
    "IngestedEvidence",
    "JSONAPIRouteConfig",
    "ManualEvidenceInput",
    "Platform",
    "ProvenancePayload",
    "StructuredOutputSpec",
    "UrllibReadOnlyJSONTransport",
    "default_connector_catalog",
    "build_connector_registry",
    "validate_connector_evidence",
    "validate_manual_evidence",
]
