from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string
from django.utils import timezone

from accounts.models import SecretReference as SecretReferenceRecord
from dailyops.runtime import (
    DailyOperationsRuntime,
    DailyOperationsRuntimeConfig,
    DeepSeekRuntimeConfig,
    build_daily_operations_runtime,
)
from integrations.ai.budget import BudgetGuard, BudgetLimits, ModelPricing
from integrations.ai.secrets import SecretFileReference
from integrations.ai.transport import HTTPTransport
from integrations.connectors.runtime import ConnectorRuntimeConfig


class _DeploymentValues:
    def __init__(self, settings_values: Any, environment_values: Mapping[str, str]):
        self.settings_values = settings_values
        self.environment_values = environment_values

    def value(self, name: str, default: Any = None) -> Any:
        missing = object()
        if isinstance(self.settings_values, Mapping):
            value = self.settings_values.get(name, missing)
        else:
            value = getattr(self.settings_values, name, missing)
        if value is not missing:
            return value
        return self.environment_values.get(name, default)


def _setting(values: Any, name: str, default: Any = None) -> Any:
    if isinstance(values, _DeploymentValues):
        return values.value(name, default)
    if isinstance(values, Mapping):
        return values.get(name, default)
    return getattr(values, name, default)


def _enabled(values: Any, name: str, default: bool = False) -> bool:
    value = _setting(values, name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"0", "false", "no", "off", ""}:
        return False
    raise ImproperlyConfigured(f"{name} must be an explicit boolean")


def _decimal_setting(values: Any, name: str, *, strictly_positive: bool = False) -> Decimal:
    value = _setting(values, name)
    if value in (None, ""):
        raise ImproperlyConfigured(f"{name} is required for live DeepSeek")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ImproperlyConfigured(f"{name} must be a decimal") from exc
    if parsed < 0:
        raise ImproperlyConfigured(f"{name} cannot be negative")
    if strictly_positive and parsed == 0:
        raise ImproperlyConfigured(f"{name} must be greater than zero for the hosted DeepSeek API")
    return parsed


def _integer_setting(values: Any, name: str) -> int:
    value = _setting(values, name)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ImproperlyConfigured(f"{name} cannot be negative")
    return parsed


def _environment_scope(values: Any) -> str:
    value = str(
        _setting(values, "DEPLOYMENT_STAGE", _setting(values, "GROWTH_OS_ENV", "LOCAL"))
    ).upper()
    aliases = {
        "LOCAL": "LOCAL",
        "STAGING": "STAGING",
        "STAGING-CANDIDATE": "STAGING",
        "STAGING_CANDIDATE": "STAGING",
        "PRODUCTION": "PRODUCTION",
    }
    if value not in aliases:
        raise ImproperlyConfigured(
            "DEPLOYMENT_STAGE must be LOCAL, STAGING/STAGING-CANDIDATE, or PRODUCTION"
        )
    return aliases[value]


def _resolve_secret_record(values: Any, supplied: SecretReferenceRecord | None) -> SecretReferenceRecord:
    record = supplied
    if record is None:
        reference_id = _setting(values, "DAILYOPS_DEEPSEEK_SECRET_REFERENCE_ID")
        if not reference_id:
            raise ImproperlyConfigured(
                "Live DeepSeek requires DAILYOPS_DEEPSEEK_SECRET_REFERENCE_ID or an explicit SecretReference"
            )
        try:
            record = SecretReferenceRecord.objects.get(pk=reference_id)
        except (SecretReferenceRecord.DoesNotExist, ValueError) as exc:
            raise ImproperlyConfigured("The configured DeepSeek SecretReference does not exist") from exc
    if record.status != SecretReferenceRecord.Status.ACTIVE:
        raise ImproperlyConfigured("The DeepSeek SecretReference is not ACTIVE")
    if record.expires_at is not None and record.expires_at <= timezone.now():
        raise ImproperlyConfigured("The DeepSeek SecretReference is expired")
    if record.environment_scope != _environment_scope(values):
        raise ImproperlyConfigured("The DeepSeek SecretReference belongs to another deployment stage")
    if record.backend != SecretReferenceRecord.Backend.FILE_MOUNT:
        raise ImproperlyConfigured(
            "This composition supports only an explicit read-only secret file mount; inject an external-manager resolver separately"
        )
    if record.provider_code.lower() != "deepseek":
        raise ImproperlyConfigured("The selected SecretReference is not a DeepSeek reference")
    return record


def build_deployment_daily_operations_runtime(
    settings_values: Any,
    *,
    environment_values: Mapping[str, str] | None = None,
    ai_secret_reference: SecretReferenceRecord | None = None,
    ai_transport: HTTPTransport | None = None,
    connector_config: ConnectorRuntimeConfig | None = None,
) -> DailyOperationsRuntime:
    """Compose deployment adapters from explicit, reviewable inputs.

    This factory never reads a secret and never creates a network transport.
    A live transport and exact connector route configs must be injected by the
    deployment composition root.  Missing routes remain fail-closed instead of
    guessing provider endpoints or API versions.
    """

    values = _DeploymentValues(
        settings_values,
        os.environ if environment_values is None else environment_values,
    )
    connector_enabled = _enabled(values, "DAILYOPS_CONNECTORS_ENABLED", False)
    if connector_enabled and connector_config is None:
        raise ImproperlyConfigured(
            "DAILYOPS_CONNECTORS_ENABLED requires explicit ConnectorRuntimeConfig; endpoints are never guessed"
        )
    connectors = connector_config or ConnectorRuntimeConfig()

    ai_enabled = _enabled(values, "DAILYOPS_DEEPSEEK_ENABLED", False)
    if not ai_enabled:
        return build_daily_operations_runtime(
            DailyOperationsRuntimeConfig(connectors=connectors)
        )

    record = _resolve_secret_record(values, ai_secret_reference)
    secret_setting_name = f"{record.reference_name}_FILE"
    secret_path_value = _setting(values, secret_setting_name)
    if not secret_path_value:
        raise ImproperlyConfigured(f"{secret_setting_name} must point to the read-only secret mount")
    secret_path = Path(str(secret_path_value))
    if not secret_path.is_absolute():
        raise ImproperlyConfigured(f"{secret_setting_name} must be an absolute path")
    if ai_transport is None:
        raise ImproperlyConfigured("Live DeepSeek requires an explicitly enabled injected HTTP transport")

    pricing = ModelPricing(
        input_usd_per_million_tokens=_decimal_setting(
            values,
            "DAILYOPS_DEEPSEEK_INPUT_USD_PER_MILLION",
            strictly_positive=True,
        ),
        output_usd_per_million_tokens=_decimal_setting(
            values,
            "DAILYOPS_DEEPSEEK_OUTPUT_USD_PER_MILLION",
            strictly_positive=True,
        ),
    )
    budget = BudgetGuard(
        BudgetLimits(
            max_requests=_integer_setting(values, "DAILYOPS_DEEPSEEK_MAX_REQUESTS"),
            max_cost_usd=_decimal_setting(values, "DAILYOPS_DEEPSEEK_MAX_COST_USD"),
        )
    )
    return build_daily_operations_runtime(
        DailyOperationsRuntimeConfig(
            ai=DeepSeekRuntimeConfig(
                enabled=True,
                model=str(_setting(values, "DAILYOPS_DEEPSEEK_MODEL", "deepseek-v4-flash")),
                secret=SecretFileReference(secret_path),
                pricing=pricing,
                budget=budget,
                transport=ai_transport,
                timeout_seconds=float(_setting(values, "DAILYOPS_DEEPSEEK_TIMEOUT_SECONDS", 30)),
            ),
            connectors=connectors,
        )
    )


def build_web_daily_operations_runtime(
    settings_values: Any,
    *,
    environment_values: Mapping[str, str] | None = None,
) -> DailyOperationsRuntime:
    """Resolve the trusted web composition hook, defaulting safely offline.

    A deployment that has reviewed exact connector routes/transports can point
    ``DAILYOPS_RUNTIME_FACTORY`` at a callable accepting the Django settings
    object.  Merely setting a live flag never creates a guessed transport.
    """

    values = _DeploymentValues(
        settings_values,
        os.environ if environment_values is None else environment_values,
    )
    factory_path = _setting(values, "DAILYOPS_RUNTIME_FACTORY", "")
    if not factory_path:
        return build_deployment_daily_operations_runtime(
            settings_values,
            environment_values=values.environment_values,
        )
    try:
        factory = import_string(str(factory_path))
        runtime = factory(settings_values)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise ImproperlyConfigured("DAILYOPS_RUNTIME_FACTORY could not build the reviewed runtime") from exc
    if not isinstance(runtime, DailyOperationsRuntime):
        raise ImproperlyConfigured("DAILYOPS_RUNTIME_FACTORY must return DailyOperationsRuntime")
    return runtime


__all__ = [
    "build_deployment_daily_operations_runtime",
    "build_web_daily_operations_runtime",
]
