"""Fail-closed integration exceptions with secret-safe messages."""


class IntegrationError(Exception):
    """Base class for external execution failures."""


class IntegrationConfigurationError(IntegrationError):
    """The adapter was not configured safely enough to run."""


class NetworkAccessDisabled(IntegrationConfigurationError):
    """A live transport was requested while networking is disabled."""


class SecretLoadingError(IntegrationConfigurationError):
    """A secret reference could not be loaded safely."""


class BudgetExceeded(IntegrationError):
    """A request would exceed its request-count or cost budget."""


class ProviderResponseError(IntegrationError):
    """A provider returned an invalid or unsuccessful response."""


class StructuredOutputError(ProviderResponseError):
    """A provider response did not match the requested JSON contract."""


class ConnectorConfigurationError(IntegrationConfigurationError):
    """A connector route or fallback declaration is invalid."""


class BrowserWorkerProtocolError(IntegrationError):
    """A browser worker envelope violates the pairing protocol."""


class IngestionValidationError(IntegrationError):
    """One or more imported evidence rows failed validation."""

    def __init__(self, errors: list[str] | tuple[str, ...]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))
