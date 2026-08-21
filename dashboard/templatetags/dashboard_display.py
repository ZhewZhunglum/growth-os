from __future__ import annotations

from django import template
from django.utils.translation import get_language


register = template.Library()


TASK_STATE_LABELS = {
    "DRAFT": "草稿",
    "BLOCKED": "已阻塞",
    "READY": "可分配",
    "ASSIGNED": "已分配",
    "IN_PROGRESS": "执行中",
    "SUBMITTED": "已提交",
    "UNDER_REVIEW": "审核中",
    "HUMAN_REWORK": "需要修改",
    "APPROVED": "审核通过",
    "DONE": "已完成",
    "CANCELLED": "已取消",
}

CHECK_RESULT_LABELS = {
    "PASS": "已通过",
    "FAIL": "未通过",
    "BLOCKED": "尚未准备好",
    "ERROR": "检查异常",
}

REVIEW_DECISION_LABELS = {
    "APPROVED": "审核通过",
    "CHANGES_REQUESTED": "需要修改",
    "REJECTED": "未通过审核",
}

PUBLICATION_STATUS_LABELS = {
    "GATE_PENDING": "等待门禁检查",
    "GATE_BLOCKED": "门禁未通过",
    "READY_FOR_MANUAL_PUBLISH": "可以人工发布",
    "MANUAL_PUBLISHED_RECORDED": "已记录人工发布证明",
}

PRINCIPAL_TYPE_LABELS = {
    "HUMAN_USER": "团队成员",
    "SERVICE_ACCOUNT": "系统服务账号",
    "API_CLIENT": "接口客户端",
    "SYSTEM": "系统",
}

ROLE_LABELS = {
    "OWNER": "负责人",
    "OPERATIONS_ADMIN": "运营管理员",
    "OPERATOR": "执行人员",
}

GRANT_ACTION_LABELS = {
    "VIEW": "查看",
    "EDIT": "编辑",
    "CREATE_TASK": "创建任务",
    "ASSIGN_TASK": "分配任务",
    "CANCEL_TASK": "取消任务",
    "COMPLETE_TASK": "完成任务",
    "REVIEW": "审核",
    "APPROVE": "批准",
    "PUBLISH": "发布",
    "MANAGE_ACCOUNT": "管理员工和账号",
    "EMERGENCY_STOP": "紧急停止",
    "COLLECT_READ_ONLY": "只读采集",
}

GRANT_SCOPE_LABELS = {
    "GLOBAL": "全部业务",
    "PRODUCT": "一个产品",
    "PLATFORM": "一个平台",
    "ACCOUNT": "一个发布账号",
    "SURFACE": "一个页面或功能",
}

GRANT_EFFECT_LABELS = {"ALLOW": "允许", "DENY": "明确拒绝"}
GRANT_RISK_LABELS = {"LOW": "低风险", "MEDIUM": "中风险", "HIGH": "高风险", "CRITICAL": "严重风险"}
GRANT_STATUS_LABELS = {"ACTIVE": "有效记录", "REVOKED": "已撤销", "EXPIRED": "已过期", "SUSPENDED": "已暂停"}

CRITERION_LABELS = {
    "Primary deliverable": "本次主要交付内容是否完整？",
    "primary deliverable": "本次主要交付内容是否完整？",
}


@register.filter
def task_state_zh(value: object) -> str:
    raw = str(value or "")
    return TASK_STATE_LABELS.get(raw, raw)


@register.filter
def check_result_zh(value: object) -> str:
    raw = str(value or "")
    return CHECK_RESULT_LABELS.get(raw, raw)


@register.filter
def review_decision_zh(value: object) -> str:
    raw = str(value or "")
    return REVIEW_DECISION_LABELS.get(raw, raw)


@register.filter
def publication_status_zh(value: object) -> str:
    raw = str(value or "")
    return PUBLICATION_STATUS_LABELS.get(raw, raw)


@register.filter
def principal_type_zh(value: object) -> str:
    raw = str(value or "")
    return PRINCIPAL_TYPE_LABELS.get(raw, raw)


@register.filter
def role_zh(value: object) -> str:
    raw = str(value or "")
    return ROLE_LABELS.get(raw, raw)


@register.filter
def grant_action_zh(value: object) -> str:
    raw = str(value or "")
    return GRANT_ACTION_LABELS.get(raw, raw)


@register.filter
def grant_scope_zh(value: object) -> str:
    raw = str(value or "")
    return GRANT_SCOPE_LABELS.get(raw, raw)


@register.filter
def grant_effect_zh(value: object) -> str:
    raw = str(value or "")
    return GRANT_EFFECT_LABELS.get(raw, raw)


@register.filter
def grant_risk_zh(value: object) -> str:
    raw = str(value or "")
    return GRANT_RISK_LABELS.get(raw, raw)


@register.filter
def grant_status_zh(value: object) -> str:
    raw = str(value or "")
    return GRANT_STATUS_LABELS.get(raw, raw)


@register.filter
def criterion_label_zh(value: object) -> str:
    raw = str(value or "")
    return CRITERION_LABELS.get(raw, raw)


@register.filter
def is_criterion_field(bound_field: object) -> bool:
    return str(getattr(bound_field, "name", "")).startswith("criterion__")


@register.simple_tag
def render_delivery_check(bound_field):
    """Render a DoD choice with plain-language labels while preserving values."""

    field = bound_field.field
    original_choices = field.choices
    try:
        if str(get_language() or "zh-hans").lower().startswith("en"):
            field.choices = (
                ("", "Select delivery status"),
                ("PASS", "Confirm complete delivery and send for review"),
                ("BLOCKED", "Not ready; do not send for review"),
            )
        else:
            field.choices = (
                ("", "请选择交付状态"),
                ("PASS", "确认完整交付并送审"),
                ("BLOCKED", "尚未准备好，本次不送审"),
            )
        return bound_field.as_widget()
    finally:
        field.choices = original_choices
