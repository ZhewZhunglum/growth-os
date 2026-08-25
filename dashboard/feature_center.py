from __future__ import annotations

from dataclasses import dataclass

from django.urls import NoReverseMatch, reverse

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from dashboard.action_center import ActionCenter, build_action_center
from products.models import Product


@dataclass(frozen=True, slots=True)
class FeatureLink:
    key: str
    label_zh: str
    label_en: str
    hint_zh: str
    hint_en: str
    url: str


@dataclass(frozen=True, slots=True)
class FeatureGroup:
    key: str
    label_zh: str
    label_en: str
    hint_zh: str
    hint_en: str
    links: tuple[FeatureLink, ...]


@dataclass(frozen=True, slots=True)
class FeatureCenter:
    groups: tuple[FeatureGroup, ...]
    can_open_geo: bool


def _safe_reverse(name: str, *, fragment: str = "") -> str:
    """Keep the navigation projection usable in reduced test URLConfs."""

    try:
        return f"{reverse(name)}{fragment}"
    except NoReverseMatch:
        return "#"


def _can_use_any_product(user: Principal, action: str) -> bool:
    products = Product.objects.filter(product_status=Product.ProductStatus.ACTIVE).order_by("pk")
    return any(
        resolve_authorization(
            principal=user,
            acting_role=user.role,
            action=action,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=product,
        ).allowed
        for product in products
    )


def _can_use_global(user: Principal, action: str) -> bool:
    return resolve_authorization(
        principal=user,
        acting_role=user.role,
        action=action,
        scope_kind=PermissionGrant.ScopeKind.GLOBAL,
    ).allowed


def build_feature_center(
    user: Principal,
    *,
    action_center: ActionCenter | None = None,
) -> FeatureCenter:
    """Return discoverable features filtered by their existing live grants.

    This is navigation only. Destination views keep enforcing their own exact
    permissions; a role name never unlocks a feature by itself.
    """

    if not getattr(user, "is_authenticated", False) or not user.can_authenticate:
        return FeatureCenter(groups=(), can_open_geo=False)

    action_center = action_center or build_action_center(user)
    can_view_product = _can_use_any_product(user, PermissionGrant.Action.VIEW)
    can_edit_product = _can_use_any_product(user, PermissionGrant.Action.EDIT)
    can_open_geo = _can_use_any_product(user, PermissionGrant.Action.COLLECT_READ_ONLY)
    can_manage_team = bool(
        user.role != Principal.Role.OPERATOR
        and _can_use_global(user, PermissionGrant.Action.MANAGE_ACCOUNT)
    )
    can_open_governance = _can_use_global(user, PermissionGrant.Action.VIEW)

    groups: list[FeatureGroup] = []

    planning_links: list[FeatureLink] = []
    if can_view_product or can_edit_product:
        planning_links.append(
            FeatureLink(
                key="opportunities",
                label_zh="找机会并安排工作",
                label_en="Find opportunities and plan work",
                hint_zh="从平台线索开始，确认机会、平台和执行账号。",
                hint_en="Start with platform signals, then confirm the opportunity, channel, and account.",
                url=_safe_reverse("dailyops:home"),
            )
        )
    if planning_links:
        groups.append(
            FeatureGroup(
                key="plan",
                label_zh="第 1 阶段 · 找机会与安排",
                label_en="Stage 1 · Find and plan",
                hint_zh="决定今天做什么，并把工作交给合适的人。",
                hint_en="Decide what to do today and assign it to the right person.",
                links=tuple(planning_links),
            )
        )

    execution_links = [
        FeatureLink(
            key="tasks",
            label_zh="执行我的任务",
            label_en="Do my assigned work",
            hint_zh="查看我创建或分配给我的任务。",
            hint_en="See work I created or that was assigned to me.",
            url=_safe_reverse("dashboard:home", fragment="#my-tasks"),
        )
    ]
    if action_center.can_open_review:
        execution_links.append(
            FeatureLink(
                key="review",
                label_zh="审核别人提交的内容",
                label_en="Review submitted content",
                hint_zh="这里只出现我有权审核、且不是我自己提交的内容。",
                hint_en="Only content I may review and did not submit myself appears here.",
                url=_safe_reverse("dashboard:review-queue"),
            )
        )
    if action_center.can_open_publish or action_center.can_open_complete:
        execution_links.append(
            FeatureLink(
                key="publish",
                label_zh="发布与确认完成",
                label_en="Publish and confirm completion",
                hint_zh="复制已审核内容，完成发布检查并记录发布结果。",
                hint_en="Copy approved content, run release checks, and record the result.",
                url=_safe_reverse("dashboard:release-queue"),
            )
        )
    groups.append(
        FeatureGroup(
            key="execute",
            label_zh="第 2 阶段 · 执行、审核与发布",
            label_en="Stage 2 · Execute, review, and publish",
            hint_zh="系统按当前权限只显示我能做的动作。",
            hint_en="The system only shows actions allowed by my current permissions.",
            links=tuple(execution_links),
        )
    )

    results_links: list[FeatureLink] = []
    if can_open_geo:
        results_links.append(
            FeatureLink(
                key="geo",
                label_zh="AI 搜索曝光（GEO）",
                label_en="AI search visibility (GEO)",
                hint_zh="测试 DeepSeek、ChatGPT 等是否提到或引用 PUKO。",
                hint_en="Test whether DeepSeek, ChatGPT, and others mention or cite PUKO.",
                url=_safe_reverse("feedback:home", fragment="#geo"),
            )
        )
        results_links.append(
            FeatureLink(
                key="performance-learning",
                label_zh="平台表现与改进建议",
                label_en="Platform results and improvement proposals",
                hint_zh="记录发布后的表现，并把结论保存为建议，不会自动改规则。",
                hint_en="Record post-publication results and save proposals without changing rules automatically.",
                url=_safe_reverse("feedback:home", fragment="#results-learning"),
            )
        )
    if can_open_governance:
        results_links.append(
            FeatureLink(
                key="governance",
                label_zh="问题与规则治理",
                label_en="Issues and rule governance",
                hint_zh="记录问题、会议结论和受控规则提案。",
                hint_en="Record issues, meeting decisions, and governed rule proposals.",
                url=_safe_reverse("governanceui:home"),
            )
        )
    if results_links:
        groups.append(
            FeatureGroup(
                key="learn",
                label_zh="第 3 阶段 · 看结果与学习",
                label_en="Stage 3 · Review results and learn",
                hint_zh="发布后再记录结果；GEO 是 AI 搜索曝光，不是普通搜索排名。",
                hint_en="Record results after publishing. GEO measures AI-search visibility, not ordinary search ranking.",
                links=tuple(results_links),
            )
        )

    management_links: list[FeatureLink] = []
    if can_manage_team:
        management_links.append(
            FeatureLink(
                key="team",
                label_zh="员工与权限",
                label_en="Staff and permissions",
                hint_zh="查看员工账号，并在现有边界内管理权限。",
                hint_en="View staff accounts and manage permissions within existing boundaries.",
                url=_safe_reverse("dashboard:team-members"),
            )
        )
    if can_edit_product or can_manage_team:
        management_links.append(
            FeatureLink(
                key="configuration",
                label_zh="产品、账号与运行设置",
                label_en="Product, account, and runtime setup",
                hint_zh="维护封存产品资料、平台账号和运行环境。",
                hint_en="Maintain sealed product profiles, platform accounts, and runtime environments.",
                url=_safe_reverse("dashboard:configuration-home"),
            )
        )
    management_links.append(
        FeatureLink(
            key="guide",
            label_zh="使用说明",
            label_en="User guide",
            hint_zh="不懂术语或下一步时，从这里查看大白话说明。",
            hint_en="Use the plain-language guide when a term or next step is unclear.",
            url=_safe_reverse("dashboard:guide"),
        )
    )
    groups.append(
        FeatureGroup(
            key="manage",
            label_zh="团队与系统",
            label_en="Team and system",
            hint_zh="这些不是每日必做；需要时再进入。",
            hint_en="These are not daily steps; open them only when needed.",
            links=tuple(management_links),
        )
    )

    return FeatureCenter(groups=tuple(groups), can_open_geo=can_open_geo)
