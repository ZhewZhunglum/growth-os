from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from releasegate.models import AccountEnvironmentBinding, CapabilityState, ChannelAccount


class ManualPublishReadiness(str, Enum):
    READY = "READY"
    ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"
    NO_ENVIRONMENT = "NO_ENVIRONMENT"
    MULTIPLE_ENVIRONMENTS = "MULTIPLE_ENVIRONMENTS"
    NO_CAPABILITY = "NO_CAPABILITY"
    CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"
    CAPABILITY_CLOSED = "CAPABILITY_CLOSED"
    CAPABILITY_EXPIRED = "CAPABILITY_EXPIRED"
    CAPABILITY_NOT_EFFECTIVE = "CAPABILITY_NOT_EFFECTIVE"


@dataclass(frozen=True, slots=True)
class ManualPublishContext:
    readiness: ManualPublishReadiness
    binding: AccountEnvironmentBinding | None = None
    capability: CapabilityState | None = None
    validation_message: str = ""

    @property
    def ready(self) -> bool:
        return self.readiness is ManualPublishReadiness.READY


def inspect_manual_publish_context(
    channel_account: ChannelAccount,
    *,
    at=None,
) -> ManualPublishContext:
    """Inspect the exact current manual-publish context without changing state.

    This is the shared projection used by both planning and settings.  It must
    stay fail-closed: no current binding, more than one current environment, or
    anything other than one current OPEN capability is not ready.
    """

    at = at or timezone.now()
    if channel_account.status != ChannelAccount.Status.ACTIVE:
        return ManualPublishContext(
            ManualPublishReadiness.ACCOUNT_INACTIVE,
            validation_message="所选账号当前不可用。",
        )

    bindings = (
        AccountEnvironmentBinding.objects.select_related("runtime_environment")
        .filter(
            channel_account_id=channel_account.pk,
            status=AccountEnvironmentBinding.Status.ACTIVE,
            valid_from__lte=at,
            runtime_environment__status="ACTIVE",
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=at))
        .order_by("runtime_environment__environment_code", "-binding_version", "id")
    )
    current_bindings = [binding for binding in bindings if binding.is_current_at(at)]
    if not current_bindings:
        return ManualPublishContext(
            ManualPublishReadiness.NO_ENVIRONMENT,
            validation_message="这个账号当前没有可用的运行环境，请先完成账号与环境配置。",
        )
    if len(current_bindings) > 1:
        return ManualPublishContext(
            ManualPublishReadiness.MULTIPLE_ENVIRONMENTS,
            validation_message="这个账号同时连接了多个运行环境，系统不能安全猜选；请先只保留一个当前绑定。",
        )

    binding = current_bindings[0]
    capability = (
        CapabilityState.objects.filter(
            account_environment_binding=binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
        )
        .order_by("-state_version")
        .first()
    )
    if capability is None:
        return ManualPublishContext(
            ManualPublishReadiness.NO_CAPABILITY,
            binding=binding,
            validation_message="这个账号当前没有人工发布能力配置，请先完成运行配置。",
        )
    if capability.is_current_open_at(at):
        return ManualPublishContext(
            ManualPublishReadiness.READY,
            binding=binding,
            capability=capability,
        )

    if capability.state == CapabilityState.State.UNKNOWN:
        readiness = ManualPublishReadiness.CAPABILITY_UNKNOWN
    elif capability.state == CapabilityState.State.CLOSED:
        readiness = ManualPublishReadiness.CAPABILITY_CLOSED
    elif capability.effective_from > at:
        readiness = ManualPublishReadiness.CAPABILITY_NOT_EFFECTIVE
    else:
        readiness = ManualPublishReadiness.CAPABILITY_EXPIRED
    return ManualPublishContext(
        readiness,
        binding=binding,
        capability=capability,
        validation_message="这个账号当前不能人工发布，请先检查账号能力状态。",
    )


def resolve_manual_publish_context(
    channel_account: ChannelAccount,
    *,
    at=None,
) -> tuple[AccountEnvironmentBinding, CapabilityState]:
    inspection = inspect_manual_publish_context(channel_account, at=at)
    if not inspection.ready:
        raise ValidationError(inspection.validation_message)
    assert inspection.binding is not None
    assert inspection.capability is not None
    return inspection.binding, inspection.capability


__all__ = [
    "ManualPublishContext",
    "ManualPublishReadiness",
    "inspect_manual_publish_context",
    "resolve_manual_publish_context",
]
