from __future__ import annotations

from django.core.exceptions import ValidationError

from integrations.connectors.types import Platform


# Platforms where Daily Operations may create a ChannelPlan and an execution
# Task.  Search/GSC/GA4 remain valid collection and analysis sources, but they
# are deliberately not publishing destinations.
EXECUTION_PLATFORMS = (
    Platform.TIKTOK,
    Platform.PINTEREST,
    Platform.QUORA,
    Platform.SHOPIFY,
)
ANALYTICAL_PLATFORMS = (
    Platform.GOOGLE_SEARCH,
    Platform.GOOGLE_SEARCH_CONSOLE,
    Platform.GOOGLE_ANALYTICS_4,
)
EXECUTION_PLATFORM_VALUES = frozenset(platform.value for platform in EXECUTION_PLATFORMS)
ANALYTICAL_PLATFORM_VALUES = frozenset(platform.value for platform in ANALYTICAL_PLATFORMS)


def require_execution_platform(platform: Platform | str) -> Platform:
    """Return a normalized execution platform or fail closed.

    This validator belongs in the service layer as well as the form.  A forged
    POST, management command, or future connector therefore cannot turn an
    analytical source into a publishing task.
    """

    try:
        normalized = platform if isinstance(platform, Platform) else Platform(str(platform))
    except ValueError as error:
        raise ValidationError("不支持这个平台，不能建立执行任务。") from error
    if normalized not in EXECUTION_PLATFORMS:
        raise ValidationError(
            "Google Search、GSC 和 GA4 只用于分析数据，不能建立发布或执行任务。"
        )
    return normalized


__all__ = [
    "ANALYTICAL_PLATFORMS",
    "ANALYTICAL_PLATFORM_VALUES",
    "EXECUTION_PLATFORMS",
    "EXECUTION_PLATFORM_VALUES",
    "require_execution_platform",
]
