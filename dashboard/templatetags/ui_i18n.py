"""Presentation-only Chinese/English helpers.

The selected language comes from Django's session-backed LocaleMiddleware.
These tags never translate stored business values, enums, audit text, task
contracts, review decisions, or any other database payload.
"""

from django import template


register = template.Library()


TASK_STATE_LABELS = {
    "DRAFT": ("待准备", "Needs preparation"),
    "BLOCKED": ("遇到阻塞", "Blocked"),
    "READY": ("待分配", "Ready to assign"),
    "ASSIGNED": ("待开始", "Ready to start"),
    "IN_PROGRESS": ("进行中", "In progress"),
    "HUMAN_REWORK": ("需要修改", "Changes requested"),
    "UNDER_REVIEW": ("审核中", "In review"),
    "APPROVED": ("审核通过", "Approved"),
    "DONE": ("已完成", "Done"),
    "CANCELLED": ("已取消", "Cancelled"),
}

NEXT_ACTION_LABELS = {
    "DRAFT": ("完成开工准备", "Prepare the task"),
    "BLOCKED": ("处理阻塞", "Resolve blocker"),
    "READY": ("分配执行人", "Assign operator"),
    "ASSIGNED": ("开始任务", "Start task"),
    "IN_PROGRESS": ("继续并提交", "Continue and submit"),
    "HUMAN_REWORK": ("按意见修改", "Revise the work"),
}

ROLE_LABELS = {
    "OWNER": ("负责人", "Owner"),
    "OPERATIONS_ADMIN": ("运营管理员", "Operations admin"),
    "OPERATOR": ("执行人员", "Operator"),
}

DAILY_STATE_LABELS = {
    "PROPOSED": ("等你决定", "Awaiting your decision"),
    "TRIAGED": ("初步判断完成", "Initial review complete"),
    "APPROVED": ("已批准", "Approved"),
    "REJECTED": ("已决定不做", "Not pursuing"),
    "PLANNED": ("已进入执行计划", "Added to the plan"),
    "CLOSED": ("已结束", "Closed"),
    "DRAFT": ("草稿", "Draft"),
    "READY": ("准备完成", "Ready"),
    "ACTIVE": ("进行中", "Active"),
    "COMPLETED": ("已完成", "Completed"),
    "CANCELLED": ("已取消", "Cancelled"),
}

CHECK_RESULT_LABELS = {
    "PASS": ("已通过", "Passed"),
    "FAIL": ("未通过", "Failed"),
    "BLOCKED": ("尚未准备好", "Not ready"),
    "ERROR": ("检查异常", "Check error"),
}

REVIEW_DECISION_LABELS = {
    "APPROVED": ("审核通过", "Approved"),
    "CHANGES_REQUESTED": ("需要修改", "Changes requested"),
    "REJECTED": ("未通过审核", "Rejected"),
}

PUBLICATION_STATUS_LABELS = {
    "GATE_PENDING": ("等待门禁检查", "Waiting for release checks"),
    "GATE_BLOCKED": ("门禁未通过", "Release checks blocked"),
    "READY_FOR_MANUAL_PUBLISH": ("可以人工发布", "Ready for manual publishing"),
    "MANUAL_PUBLISHED_RECORDED": ("已记录人工发布证明", "Manual publication recorded"),
}

# Presentation labels for stored system enums.  Stored values and user-entered
# business text remain untouched.
SYSTEM_VALUE_LABELS = {
    "OPEN": ("待处理", "Open"),
    "TRIAGED": ("已初步判断", "Triaged"),
    "IN_PROGRESS": ("处理中", "In progress"),
    "RESOLVED": ("已解决", "Resolved"),
    "CLOSED": ("已关闭", "Closed"),
    "ESCALATED_TO_MEETING": ("已升级到会议", "Escalated to meeting"),
    "OPERATIONAL": ("运营问题", "Operational"),
    "BLOCKER": ("阻塞", "Blocker"),
    "SAFETY_EVENT": ("安全事件", "Safety event"),
    "RULE_CONFLICT": ("规则冲突", "Rule conflict"),
    "LOW": ("低", "Low"),
    "MEDIUM": ("中", "Medium"),
    "HIGH": ("高", "High"),
    "CRITICAL": ("严重", "Critical"),
    "OPERATIONAL_REVIEW": ("运营复盘", "Operational review"),
    "RULE_GOVERNANCE": ("规则治理", "Rule governance"),
    "SAFETY_INCIDENT": ("安全事件", "Safety incident"),
    "RULE_PROPOSAL": ("规则提案", "Rule proposal"),
    "OPERATIONAL_FIX": ("运营修正", "Operational fix"),
    "NO_ACTION": ("无需行动", "No action"),
    "TIGHTEN": ("收紧", "Tighten"),
    "RELAX": ("放松", "Relax"),
    "CLARIFY": ("澄清", "Clarify"),
    "ADD": ("新增", "Add"),
    "RETIRE": ("停用", "Retire"),
    "HISTORICAL_REPLAY": ("历史回放", "Historical replay"),
    "SHADOW": ("影子验证", "Shadow"),
    "CANARY": ("小范围试运行", "Canary"),
    "PASSED": ("通过", "Passed"),
    "FAILED": ("失败", "Failed"),
    "PARTIAL": ("部分通过", "Partial"),
    "APPROVED": ("已批准", "Approved"),
    "REJECTED": ("已拒绝", "Rejected"),
    "CHANGES_REQUESTED": ("需要修改", "Changes requested"),
    "MEETING_DECISION": ("会议结论", "Meeting decision"),
    "LEARNING": ("学习结论", "Learning"),
    "ISSUE": ("问题", "Issue"),
    "OFFICIAL_POLICY": ("官方政策", "Official policy"),
    "MANUAL": ("人工提出", "Manual"),
}


def _is_english(context) -> bool:
    request = context.get("request")
    language_code = getattr(request, "LANGUAGE_CODE", "zh-hans")
    return str(language_code).lower().startswith("en")


@register.simple_tag(takes_context=True)
def ui(context, chinese: str, english: str) -> str:
    """Return one of two static UI labels for the current session."""

    return english if _is_english(context) else chinese


@register.simple_tag(takes_context=True)
def ui_language_code(context) -> str:
    """Return a valid HTML language tag without exposing business locale."""

    return "en" if _is_english(context) else "zh-CN"


def _localized_pair(context, pair: tuple[str, str] | None, fallback: object) -> str:
    if pair is None:
        return str(fallback or "")
    return pair[1] if _is_english(context) else pair[0]


@register.simple_tag(takes_context=True)
def ui_task_state(context, value: object) -> str:
    raw = str(value or "")
    return _localized_pair(context, TASK_STATE_LABELS.get(raw), raw)


@register.simple_tag(takes_context=True)
def ui_next_action(context, value: object) -> str:
    raw = str(value or "")
    return _localized_pair(context, NEXT_ACTION_LABELS.get(raw), raw)


@register.simple_tag(takes_context=True)
def ui_role(context, value: object) -> str:
    raw = str(value or "")
    return _localized_pair(context, ROLE_LABELS.get(raw), raw)


@register.simple_tag(takes_context=True)
def ui_daily_state(context, value: object) -> str:
    """Localize Daily Operations projections without changing stored enums."""

    raw = str(value or "")
    return _localized_pair(context, DAILY_STATE_LABELS.get(raw), raw)


@register.simple_tag(takes_context=True)
def ui_check_result(context, value: object) -> str:
    raw = str(value or "")
    return _localized_pair(context, CHECK_RESULT_LABELS.get(raw), raw)


@register.simple_tag(takes_context=True)
def ui_review_decision(context, value: object) -> str:
    raw = str(value or "")
    return _localized_pair(context, REVIEW_DECISION_LABELS.get(raw), raw)


@register.simple_tag(takes_context=True)
def ui_publication_status(context, value: object) -> str:
    raw = str(value or "")
    return _localized_pair(context, PUBLICATION_STATUS_LABELS.get(raw), raw)


@register.simple_tag(takes_context=True)
def ui_system_value(context, value: object) -> str:
    raw = str(value or "")
    return _localized_pair(context, SYSTEM_VALUE_LABELS.get(raw), raw)


@register.simple_tag(takes_context=True)
def action_center_for(context, user):
    """Reuse the home context or calculate the same protected inbox globally."""

    existing = context.get("action_center")
    if existing is not None:
        return existing
    from dashboard.action_center import build_action_center

    return build_action_center(user)
