from __future__ import annotations

from django import template


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
        field.choices = (
            ("", "请选择交付状态"),
            ("PASS", "确认完整交付并送审"),
            ("BLOCKED", "尚未准备好，本次不送审"),
        )
        return bound_field.as_widget()
    finally:
        field.choices = original_choices
