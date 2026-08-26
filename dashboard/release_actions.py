from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from accounts.authorization import require_authorization
from accounts.models import PermissionGrant, Principal
from dashboard.review_forms import ReturnToInlineContentForm, StopPublicationForm
from workflow.models import Task


def _validation_text(error: ValidationError) -> str:
    return " ".join(error.messages) if error.messages else "操作未通过校验，请刷新后重试。"


def _require_owner_or_admin(user: Principal) -> None:
    if user.role not in {
        Principal.Role.OWNER,
        Principal.Role.OPERATIONS_ADMIN,
    }:
        raise PermissionDenied("ONLY_OWNER_OR_ADMIN_CAN_CHANGE_APPROVED_PUBLICATION")


def _require_product_grant(user: Principal, task: Task, action: str):
    return require_authorization(
        principal=user,
        acting_role=user.role,
        action=action,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=task.product,
    )


@login_required
@require_POST
def release_stop_action(request: HttpRequest, task_id) -> HttpResponse:
    """Stop an approved publication append-only, without deleting its history."""

    task = get_object_or_404(
        Task.objects.select_related("product"),
        pk=task_id,
    )
    _require_owner_or_admin(request.user)
    cancel_grant = _require_product_grant(
        request.user,
        task,
        PermissionGrant.Action.CANCEL_TASK,
    )
    form = StopPublicationForm(request.POST, state_version=task.state_version)
    if not form.is_valid():
        messages.error(request, "请填写停止原因并勾选确认。")
        return redirect("dashboard:release-detail", task_id=task.pk)
    try:
        Task.stop_publication(
            task_id=task.pk,
            command_id=form.cleaned_data["command_id"],
            expected_state_version=form.cleaned_data["expected_state_version"],
            actor_principal=request.user,
            acting_role=request.user.role,
            permission_grant=cancel_grant,
            recorded_by_principal=request.user,
            reason=form.cleaned_data["reason"],
        )
    except ValidationError as error:
        messages.error(request, _validation_text(error))
        return redirect("dashboard:release-detail", task_id=task.pk)
    messages.success(request, "发布已停止；旧提交、审核和门禁历史均已保留，旧门禁不可复用。")
    return redirect("dashboard:release-queue")


@login_required
@require_POST
def release_rework_action(request: HttpRequest, task_id) -> HttpResponse:
    """Return the latest approved link-only submission for a new inline version."""

    task = get_object_or_404(
        Task.objects.select_related("product"),
        pk=task_id,
    )
    _require_owner_or_admin(request.user)
    edit_grant = _require_product_grant(
        request.user,
        task,
        PermissionGrant.Action.EDIT,
    )
    form = ReturnToInlineContentForm(request.POST, state_version=task.state_version)
    if not form.is_valid():
        messages.error(request, "请填写退回原因并勾选确认。")
        return redirect("dashboard:release-detail", task_id=task.pk)
    submission = task.submissions.order_by("-submission_number").first()
    if submission is None:
        raise Http404("Task has no sealed submission.")
    try:
        Task.return_approved_submission_for_rework(
            task_id=task.pk,
            submission_id=submission.pk,
            command_id=form.cleaned_data["command_id"],
            expected_state_version=form.cleaned_data["expected_state_version"],
            actor_principal=request.user,
            acting_role=request.user.role,
            permission_grant=edit_grant,
            recorded_by_principal=request.user,
            reason=form.cleaned_data["reason"],
        )
    except ValidationError as error:
        messages.error(request, _validation_text(error))
        return redirect("dashboard:release-detail", task_id=task.pk)
    messages.success(
        request,
        "已退回制作完整正文；旧链接、提交、审核和门禁均保留，新提交会生成下一版本。",
    )
    return redirect("dashboard:task-detail", task_id=task.pk)
