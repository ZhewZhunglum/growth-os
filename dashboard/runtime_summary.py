from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from dailyops.platforms import ANALYTICAL_PLATFORMS, EXECUTION_PLATFORMS
from integrations.connectors.types import Platform
from releasegate.models import AccountEnvironmentBinding, ChannelAccount, RuntimeEnvironment
from releasegate.runtime import ManualPublishReadiness, inspect_manual_publish_context


PLATFORM_COPY = {
    Platform.TIKTOK: (
        "TikTok",
        "TikTok",
        "短视频内容的人工发布任务",
        "Manual publishing work for short-form video",
        "TK",
    ),
    Platform.PINTEREST: (
        "Pinterest",
        "Pinterest",
        "图片、Pin 和灵感内容的人工发布任务",
        "Manual publishing work for Pins and visual content",
        "P",
    ),
    Platform.QUORA: (
        "Quora",
        "Quora",
        "问答内容的人工发布任务",
        "Manual publishing work for question-and-answer content",
        "Q",
    ),
    Platform.SHOPIFY: (
        "Shopify / 博客",
        "Shopify / blog",
        "独立站和博客内容的人工发布任务",
        "Manual publishing work for the storefront and blog",
        "S",
    ),
    Platform.GOOGLE_SEARCH: (
        "Google 搜索",
        "Google Search",
        "只用于发现和分析搜索需求，不创建发布任务",
        "Analysis only; never creates publishing work",
        "G",
    ),
    Platform.GOOGLE_SEARCH_CONSOLE: (
        "Google Search Console",
        "Google Search Console",
        "只用于分析搜索曝光与点击，不创建发布任务",
        "Analysis only for search visibility and clicks",
        "GSC",
    ),
    Platform.GOOGLE_ANALYTICS_4: (
        "Google Analytics 4",
        "Google Analytics 4",
        "只用于分析站内行为，不创建发布任务",
        "Analysis only for on-site behavior",
        "GA4",
    ),
}


@dataclass(frozen=True, slots=True)
class AccountSummary:
    account: ChannelAccount
    state: str
    state_zh: str
    state_en: str
    detail_zh: str
    detail_en: str
    environment_zh: str = ""
    environment_en: str = ""
    conflicting_contexts_zh: str = ""
    conflicting_contexts_en: str = ""
    repair_step: str = ""
    repair_binding_id: object | str = ""
    ready: bool = False


@dataclass(frozen=True, slots=True)
class PlatformSummary:
    platform: Platform
    name_zh: str
    name_en: str
    purpose_zh: str
    purpose_en: str
    monogram: str
    anchor: str
    kind: str
    accounts: tuple[AccountSummary, ...]
    state: str
    state_zh: str
    state_en: str

    @property
    def ready(self) -> bool:
        return self.state == "READY"


@dataclass(frozen=True, slots=True)
class EnvironmentSummary:
    environment: RuntimeEnvironment
    label_zh: str
    label_en: str
    detail_zh: str
    detail_en: str
    state: str
    state_zh: str
    state_en: str


@dataclass(frozen=True, slots=True)
class CurrentEnvironmentSummary:
    environment: RuntimeEnvironment
    display_zh: str
    display_en: str


def platform_anchor(platform: Platform) -> str:
    return f"platform-{platform.value.lower().replace('_', '-')}"


def environment_labels(environment: RuntimeEnvironment) -> tuple[str, str, str, str]:
    if environment.environment_type == RuntimeEnvironment.EnvironmentType.PRODUCTION:
        return (
            "正式环境",
            "Production",
            "用于真实业务；每次发布仍需通过人工审核和发布检查。",
            "Used for real work; every release still requires human review and release checks.",
        )
    if environment.environment_code == "local-dogfood" or environment.environment_code.startswith("local-"):
        return (
            "本地练习",
            "Local practice",
            "只用于在这台电脑上练习完整流程，不会自动连接或发布到真实平台。",
            "Used to practise the full flow on this computer; it does not connect or publish automatically.",
        )
    return (
        "测试环境",
        "Test environment",
        "用于发布前测试，不应当作正式发布结果。",
        "Used for pre-release testing and not as a production result.",
    )


def current_account_environments(
    account: ChannelAccount,
    *,
    at=None,
) -> tuple[CurrentEnvironmentSummary, ...]:
    """Return each exact current environment for an account without guessing."""

    at = at or timezone.now()
    rows = AccountEnvironmentBinding.objects.select_related("runtime_environment").filter(
        channel_account_id=account.pk
    ).order_by("runtime_environment_id", "-binding_version", "-created_at", "-id")
    latest_by_environment: dict[object, AccountEnvironmentBinding] = {}
    for binding in rows:
        latest_by_environment.setdefault(binding.runtime_environment_id, binding)

    contexts = []
    for binding in latest_by_environment.values():
        environment = binding.runtime_environment
        if (
            binding.status != AccountEnvironmentBinding.Status.ACTIVE
            or binding.valid_from > at
            or (binding.valid_until is not None and binding.valid_until <= at)
            or environment.status != RuntimeEnvironment.Status.ACTIVE
        ):
            continue
        label_zh, label_en, _, _ = environment_labels(environment)
        contexts.append(
            CurrentEnvironmentSummary(
                environment=environment,
                display_zh=f"{label_zh}（{environment.environment_code}）",
                display_en=f"{label_en} ({environment.environment_code})",
            )
        )
    return tuple(sorted(contexts, key=lambda item: item.environment.environment_code))


def _execution_account_summary(account: ChannelAccount, *, at) -> AccountSummary:
    inspection = inspect_manual_publish_context(account, at=at)
    environment_zh = ""
    environment_en = ""
    is_local = False
    if inspection.binding is not None:
        environment_zh, environment_en, _, _ = environment_labels(
            inspection.binding.runtime_environment
        )
        is_local = environment_zh == "本地练习"

    copy = {
        ManualPublishReadiness.ACCOUNT_INACTIVE: (
            "INACTIVE", "已停用", "Inactive",
            "这个账号已暂停或停用；当前版本不能在此恢复，请联系系统管理员按受控流程处理。",
            "This account is suspended or retired. This version cannot restore it here; ask a system administrator to use the controlled process.",
        ),
        ManualPublishReadiness.NO_ENVIRONMENT: (
            "NEEDS_SETUP", "需要设置", "Needs setup",
            "账号还没有一个可用的使用场景。", "The account does not have an available usage context.",
        ),
        ManualPublishReadiness.MULTIPLE_ENVIRONMENTS: (
            "NEEDS_ATTENTION", "需要管理员处理", "Admin attention needed",
            "账号同时连接了多个使用场景，系统不会替你猜选。", "The account has multiple current contexts, so the system will not guess.",
        ),
        ManualPublishReadiness.NO_CAPABILITY: (
            "UNCHECKED", "尚未检查", "Not checked",
            "尚未确认这个账号能否进行人工发布。", "Manual publishing has not been confirmed for this account.",
        ),
        ManualPublishReadiness.CAPABILITY_UNKNOWN: (
            "UNCHECKED", "尚未检查", "Not checked",
            "人工发布状态还没有确认。", "Manual publishing status has not been confirmed.",
        ),
        ManualPublishReadiness.CAPABILITY_CLOSED: (
            "CLOSED", "已关闭", "Disabled",
            "这个账号当前不能用于人工发布。", "This account cannot currently be used for manual publishing.",
        ),
        ManualPublishReadiness.CAPABILITY_EXPIRED: (
            "CLOSED", "已过期", "Expired",
            "人工发布状态已经过期，需要重新检查。", "The manual-publishing status has expired and must be checked again.",
        ),
        ManualPublishReadiness.CAPABILITY_NOT_EFFECTIVE: (
            "UNCHECKED", "尚未生效", "Not active yet",
            "人工发布状态尚未到生效时间。", "The manual-publishing status is not effective yet.",
        ),
    }
    if inspection.ready:
        if is_local:
            return AccountSummary(
                account=account,
                state="READY",
                state_zh="本地练习可用",
                state_en="Local practice ready",
                detail_zh="可测试完整流程；系统不会自动替你发布到真实平台。",
                detail_en="The full flow can be tested; the system will not publish to a real platform for you.",
                environment_zh=environment_zh,
                environment_en=environment_en,
                ready=True,
            )
        return AccountSummary(
            account=account,
            state="READY",
            state_zh="可以使用",
            state_en="Ready",
            detail_zh="可以安排人工发布任务；真正发布仍由人工完成。",
            detail_en="Manual publishing work can be planned; a person still performs the actual release.",
            environment_zh=environment_zh,
            environment_en=environment_en,
            ready=True,
        )
    state, state_zh, state_en, detail_zh, detail_en = copy[inspection.readiness]
    current_contexts = (
        current_account_environments(account, at=at)
        if inspection.readiness is ManualPublishReadiness.MULTIPLE_ENVIRONMENTS
        else ()
    )
    if inspection.readiness in {
        ManualPublishReadiness.NO_ENVIRONMENT,
        ManualPublishReadiness.MULTIPLE_ENVIRONMENTS,
    }:
        repair_step = "binding"
        repair_binding_id = ""
    elif inspection.binding is not None:
        repair_step = "capability"
        repair_binding_id = inspection.binding.pk
    else:
        repair_step = ""
        repair_binding_id = ""
    return AccountSummary(
        account=account,
        state=state,
        state_zh=state_zh,
        state_en=state_en,
        detail_zh=detail_zh,
        detail_en=detail_en,
        environment_zh=environment_zh,
        environment_en=environment_en,
        conflicting_contexts_zh="、".join(context.display_zh for context in current_contexts),
        conflicting_contexts_en=", ".join(context.display_en for context in current_contexts),
        repair_step=repair_step,
        repair_binding_id=repair_binding_id,
    )


def build_platform_summaries(*, at=None) -> tuple[PlatformSummary, ...]:
    at = at or timezone.now()
    account_rows = list(ChannelAccount.objects.order_by("platform_code", "display_name", "account_code"))
    grouped = {
        platform: tuple(account for account in account_rows if account.platform_code == platform.value)
        for platform in (*EXECUTION_PLATFORMS, *ANALYTICAL_PLATFORMS)
    }
    summaries: list[PlatformSummary] = []
    for platform in EXECUTION_PLATFORMS:
        accounts = tuple(_execution_account_summary(account, at=at) for account in grouped[platform])
        if any(account.ready for account in accounts):
            state, state_zh, state_en = "READY", "可以安排任务", "Ready for planning"
        elif accounts:
            state, state_zh, state_en = "NEEDS_ATTENTION", "需要处理", "Needs attention"
        else:
            state, state_zh, state_en = "NOT_CONFIGURED", "尚未设置", "Not set up"
        name_zh, name_en, purpose_zh, purpose_en, monogram = PLATFORM_COPY[platform]
        summaries.append(
            PlatformSummary(
                platform=platform,
                name_zh=name_zh,
                name_en=name_en,
                purpose_zh=purpose_zh,
                purpose_en=purpose_en,
                monogram=monogram,
                anchor=platform_anchor(platform),
                kind="EXECUTION",
                accounts=accounts,
                state=state,
                state_zh=state_zh,
                state_en=state_en,
            )
        )
    for platform in ANALYTICAL_PLATFORMS:
        accounts = tuple(
            AccountSummary(
                account=account,
                state="REGISTERED" if account.status == ChannelAccount.Status.ACTIVE else "INACTIVE",
                state_zh="已登记" if account.status == ChannelAccount.Status.ACTIVE else "已停用",
                state_en="Registered" if account.status == ChannelAccount.Status.ACTIVE else "Inactive",
                detail_zh=(
                    "系统中已有账号记录；真实数据是否可读仍由采集步骤检查。"
                    if account.status == ChannelAccount.Status.ACTIVE
                    else "这个分析账号已暂停或停用。"
                ),
                detail_en=(
                    "An account record exists; the collection step still checks whether real data is available."
                    if account.status == ChannelAccount.Status.ACTIVE
                    else "This analysis account is suspended or retired."
                ),
            )
            for account in grouped[platform]
        )
        if any(account.state == "REGISTERED" for account in accounts):
            state, state_zh, state_en = "REGISTERED", "已登记", "Registered"
        elif accounts:
            state, state_zh, state_en = "INACTIVE", "已停用", "Inactive"
        else:
            state, state_zh, state_en = "NOT_CONFIGURED", "尚未登记", "Not registered"
        name_zh, name_en, purpose_zh, purpose_en, monogram = PLATFORM_COPY[platform]
        summaries.append(
            PlatformSummary(
                platform=platform,
                name_zh=name_zh,
                name_en=name_en,
                purpose_zh=purpose_zh,
                purpose_en=purpose_en,
                monogram=monogram,
                anchor=platform_anchor(platform),
                kind="ANALYTICAL",
                accounts=accounts,
                state=state,
                state_zh=state_zh,
                state_en=state_en,
            )
        )
    return tuple(summaries)


def build_environment_summaries() -> tuple[EnvironmentSummary, ...]:
    rows = []
    for environment in RuntimeEnvironment.objects.order_by("environment_type", "environment_code"):
        label_zh, label_en, detail_zh, detail_en = environment_labels(environment)
        if environment.status == RuntimeEnvironment.Status.ACTIVE:
            state, state_zh, state_en = "READY", "使用中", "Active"
        elif environment.status == RuntimeEnvironment.Status.LOCKED:
            state, state_zh, state_en = "LOCKED", "已锁定", "Locked"
            detail_zh = "这个使用场景已为安全保护而锁定；当前版本不能在此恢复，请联系系统管理员。"
            detail_en = "This usage context is safety-locked. This version cannot restore it here; contact a system administrator."
        else:
            state, state_zh, state_en = "INACTIVE", "已停用", "Retired"
            detail_zh = "这个使用场景已停用；当前版本不能在此恢复，请联系系统管理员。"
            detail_en = "This usage context is retired. This version cannot restore it here; contact a system administrator."
        rows.append(
            EnvironmentSummary(
                environment=environment,
                label_zh=label_zh,
                label_en=label_en,
                detail_zh=detail_zh,
                detail_en=detail_en,
                state=state,
                state_zh=state_zh,
                state_en=state_en,
            )
        )
    return tuple(rows)


__all__ = [
    "AccountSummary",
    "CurrentEnvironmentSummary",
    "EnvironmentSummary",
    "PlatformSummary",
    "build_environment_summaries",
    "build_platform_summaries",
    "current_account_environments",
    "environment_labels",
    "platform_anchor",
]
