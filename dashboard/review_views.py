from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.authorization import require_authorization, resolve_authorization
from accounts.models import PermissionGrant, Principal
from contentops.models import ReviewDecision, TaskSubmission
from dashboard.review_forms import (
    CompleteTaskForm,
    PublicationProofForm,
    ReleaseGateForm,
    ReviewDecisionForm,
)
from releasegate.models import ChannelAccount, Publication, PublicationEvent, RuntimeEnvironment
from releasegate.services import orchestrate_v1_release_gate, record_manual_publication_proof
from workflow.models import Task


MAX_PROOF_BYTES = 25 * 1024 * 1024
ALLOWED_PROOF_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp"}


def _subcommand(root: uuid.UUID, label: str) -> uuid.UUID:
    return uuid.uuid5(root, f"growth-os:dashboard-review-release:{label}")


def _validation_text(error: ValidationError) -> str:
    return " ".join(error.messages) if error.messages else "操作未通过校验，请刷新后重试。"


def _product_decision(user: Principal, task: Task, action: str):
    return resolve_authorization(
        principal=user,
        acting_role=user.role,
        action=action,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=task.product,
    )


def _require_product_grant(user: Principal, task: Task, action: str):
    return require_authorization(
        principal=user,
        acting_role=user.role,
        action=action,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=task.product,
    )


def _publish_decision(user: Principal, task: Task, account: ChannelAccount):
    return resolve_authorization(
        principal=user,
        acting_role=user.role,
        action=PermissionGrant.Action.PUBLISH,
        scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
        product=task.product,
        platform_code=account.platform_code,
        account_ref=account.account_code,
    )


def _allowed_accounts(user: Principal, task: Task):
    account_ids = [
        account.pk
        for account in ChannelAccount.objects.filter(status=ChannelAccount.Status.ACTIVE).order_by(
            "platform_code", "account_code"
        )
        if _publish_decision(user, task, account).allowed
    ]
    return ChannelAccount.objects.filter(pk__in=account_ids).order_by("platform_code", "account_code")


def _allowed_environments(accounts):
    return RuntimeEnvironment.objects.filter(
        status=RuntimeEnvironment.Status.ACTIVE,
        account_bindings__channel_account__in=accounts,
    ).distinct().order_by("environment_code")


def _can_complete(user: Principal, task: Task) -> bool:
    if not _product_decision(user, task, PermissionGrant.Action.COMPLETE_TASK).allowed:
        return False
    submission = task.submissions.order_by("-submission_number").first()
    if submission is None:
        return False
    latest_publication = submission.publications.order_by("-created_at", "-id").first()
    return bool(
        latest_publication
        and latest_publication.status == Publication.Status.MANUAL_PUBLISHED_RECORDED
    )


def _latest_submission(task: Task) -> TaskSubmission:
    submission = task.submissions.select_related(
        "primary_asset_version__content_asset",
        "dod_check_run",
    ).order_by("-submission_number").first()
    if submission is None:
        raise Http404("Task has no sealed submission.")
    return submission


def _can_review_submission(user: Principal, task: Task, submission: TaskSubmission) -> bool:
    """Return whether the user may independently review this exact submission."""

    return bool(
        submission.submitted_by_principal_id != user.pk
        and _product_decision(user, task, PermissionGrant.Action.REVIEW).allowed
    )


def _review_context(task: Task, *, form=None):
    submission = _latest_submission(task)
    return {
        "task": task,
        "submission": submission,
        "asset_version": submission.primary_asset_version,
        "review_form": form or ReviewDecisionForm(state_version=task.state_version),
    }


def _release_context(task: Task, user: Principal, *, gate_form=None, proof_form=None, done_form=None):
    submission = _latest_submission(task)
    accounts = _allowed_accounts(user, task)
    environments = _allowed_environments(accounts)
    publications = submission.publications.select_related(
        "current_gate__channel_account",
        "current_gate__runtime_environment",
        "requested_by_principal",
    ).order_by("-created_at", "-id")
    ready_publications = publications.filter(
        status=Publication.Status.READY_FOR_MANUAL_PUBLISH,
        requested_by_principal=user,
    )
    initial_publication = ready_publications.first()
    can_publish = accounts.exists()
    return {
        "task": task,
        "submission": submission,
        "publications": publications,
        "gate_form": gate_form or ReleaseGateForm(
            accounts=accounts,
            environments=environments,
            state_version=task.state_version,
        ),
        "proof_form": proof_form or (
            PublicationProofForm(
                publications=ready_publications,
                initial_publication=initial_publication,
            )
            if initial_publication and can_publish
            else None
        ),
        "done_form": done_form or (
            CompleteTaskForm(state_version=task.state_version) if _can_complete(user, task) else None
        ),
        "can_publish": can_publish,
    }


@login_required
def review_queue(request: HttpRequest) -> HttpResponse:
    tasks = []
    for task in (
        Task.objects.filter(current_state=Task.State.UNDER_REVIEW)
        .select_related("product", "contract_version", "current_assignee_principal")
        .prefetch_related("submissions")
        .order_by("updated_at", "title")
    ):
        submission = task.submissions.order_by("-submission_number").first()
        if submission is not None and _can_review_submission(request.user, task, submission):
            tasks.append(task)
    completed_reviews = (
        ReviewDecision.objects.filter(reviewer_principal=request.user)
        .select_related(
            "submission__task__product",
            "submission__primary_asset_version__content_asset",
        )
        .order_by("-decided_at", "-id")
    )
    return render(
        request,
        "dashboard/review_queue.html",
        {"tasks": tasks, "completed_reviews": completed_reviews},
    )


@login_required
def review_detail(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(
        Task.objects.select_related("product", "contract_version", "current_assignee_principal"),
        pk=task_id,
        current_state=Task.State.UNDER_REVIEW,
    )
    _require_product_grant(request.user, task, PermissionGrant.Action.REVIEW)
    submission = _latest_submission(task)
    if submission.submitted_by_principal_id == request.user.pk:
        raise PermissionDenied("SUBMITTER_CANNOT_REVIEW_OWN_SUBMISSION")
    return render(request, "dashboard/review_detail.html", _review_context(task))


@login_required
def review_history_detail(request: HttpRequest, review_id) -> HttpResponse:
    review = get_object_or_404(
        ReviewDecision.objects.select_related(
            "reviewer_principal",
            "submission__task__product",
            "submission__task__contract_version",
            "submission__primary_asset_version__content_asset",
        ),
        pk=review_id,
        reviewer_principal=request.user,
    )
    return render(
        request,
        "dashboard/review_history_detail.html",
        {
            "review": review,
            "submission": review.submission,
            "task": review.submission.task,
            "asset_version": review.submission.primary_asset_version,
        },
    )


@login_required
@require_POST
def review_action(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(Task.objects.select_related("product", "contract_version"), pk=task_id)
    review_grant = _require_product_grant(request.user, task, PermissionGrant.Action.REVIEW)
    # REVIEW and EDIT are deliberately independent grants.  The review fact
    # must not be written when the reviewer cannot project the Task state.
    edit_grant = _require_product_grant(request.user, task, PermissionGrant.Action.EDIT)
    form = ReviewDecisionForm(request.POST, state_version=task.state_version)
    if not form.is_valid():
        return render(
            request,
            "dashboard/review_detail.html",
            _review_context(task, form=form),
            status=400,
        )
    submission = _latest_submission(task)
    decision = form.cleaned_data["decision"]
    target = (
        Task.State.APPROVED
        if decision == ReviewDecision.Decision.APPROVED
        else Task.State.HUMAN_REWORK
    )
    root = form.cleaned_data["command_id"]
    try:
        with transaction.atomic():
            ReviewDecision.record_final(
                submission=submission,
                decision=decision,
                rationale=form.cleaned_data["rationale"],
                command_id=_subcommand(root, "review-decision"),
                expected_task_version=form.cleaned_data["expected_state_version"],
                reviewer_principal=request.user,
                acting_role=request.user.role,
                permission_grant=review_grant,
                recorded_by_principal=request.user,
            )
            Task.transition(
                task_id=task.pk,
                to_state=target,
                command_id=_subcommand(root, "review-transition"),
                expected_state_version=form.cleaned_data["expected_state_version"],
                actor_principal=request.user,
                acting_role=request.user.role,
                permission_grant=edit_grant,
                recorded_by_principal=request.user,
                reason=("Human review approved." if target == Task.State.APPROVED else "Human review requested changes."),
            )
        messages.success(
            request,
            "审核已通过，任务进入发布门禁。" if target == Task.State.APPROVED else "修改要求已保存，任务进入人工返工。",
        )
        return redirect("dashboard:review-queue")
    except ValidationError as error:
        messages.error(request, _validation_text(error))
    return redirect("dashboard:review-detail", task_id=task.pk)


@login_required
def release_queue(request: HttpRequest) -> HttpResponse:
    tasks = []
    for task in Task.objects.filter(current_state=Task.State.APPROVED).select_related(
        "product", "contract_version", "current_assignee_principal"
    ).order_by("updated_at", "title"):
        if _allowed_accounts(request.user, task).exists() or _can_complete(request.user, task):
            tasks.append(task)
    return render(request, "dashboard/release_queue.html", {"tasks": tasks})


@login_required
def release_detail(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(
        Task.objects.select_related("product", "contract_version", "current_assignee_principal"),
        pk=task_id,
        current_state=Task.State.APPROVED,
    )
    submission = _latest_submission(task)
    has_prior_release_fact = submission.publications.filter(
        requested_by_principal=request.user,
    ).exists()
    if (
        not _allowed_accounts(request.user, task).exists()
        and not _can_complete(request.user, task)
        and not has_prior_release_fact
    ):
        raise PermissionDenied("NO_RELEASE_QUEUE_PERMISSION")
    return render(request, "dashboard/release_detail.html", _release_context(task, request.user))


@login_required
@require_POST
def release_gate_action(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(
        Task.objects.select_related("product", "contract_version"),
        pk=task_id,
        current_state=Task.State.APPROVED,
    )
    accounts = _allowed_accounts(request.user, task)
    environments = _allowed_environments(accounts)
    form = ReleaseGateForm(
        request.POST,
        accounts=accounts,
        environments=environments,
        state_version=task.state_version,
    )
    if not form.is_valid():
        return render(
            request,
            "dashboard/release_detail.html",
            _release_context(task, request.user, gate_form=form),
            status=400,
        )
    if form.cleaned_data["expected_state_version"] != task.state_version:
        messages.error(request, "任务状态已经变化，请刷新后重新检查门禁。")
        return redirect("dashboard:release-detail", task_id=task.pk)
    try:
        result = orchestrate_v1_release_gate(
            task=task,
            submission=_latest_submission(task),
            publisher_principal=request.user,
            channel_account=form.cleaned_data["channel_account"],
            runtime_environment=form.cleaned_data["runtime_environment"],
            command_id=form.cleaned_data["command_id"],
        )
        if result.gate.outcome == "PASSED":
            messages.success(request, "门禁已通过。请你在平台上人工发布，然后回来上传证明。")
        else:
            messages.error(request, "门禁未通过，系统没有发布任何内容。请先处理页面显示的阻塞原因。")
    except ValidationError as error:
        messages.error(request, _validation_text(error))
    return redirect("dashboard:release-detail", task_id=task.pk)


def _hash_proof(uploaded) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in uploaded.chunks():
        size += len(chunk)
        if size > MAX_PROOF_BYTES:
            raise ValidationError("发布证明超过 25 MB 上限。")
        digest.update(chunk)
    if not size:
        raise ValidationError("发布证明文件不能为空。")
    uploaded.seek(0)
    return digest.hexdigest(), size


def _valid_proof_signature(uploaded, mime_type: str) -> bool:
    header = uploaded.read(12)
    uploaded.seek(0)
    if mime_type == "application/pdf":
        return header.startswith(b"%PDF-")
    if mime_type == "image/png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return header.startswith(b"\xff\xd8\xff")
    if mime_type == "image/webp":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    return False


def _delete_proof(name: str) -> None:
    try:
        if name and default_storage.exists(name):
            default_storage.delete(name)
    except Exception:
        pass


@login_required
@require_POST
def release_proof_action(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(
        Task.objects.select_related("product", "contract_version"),
        pk=task_id,
        current_state=Task.State.APPROVED,
    )
    submission = _latest_submission(task)
    # Include the terminal publication only so an identical command replay can
    # return its existing immutable proof without uploading another file.
    ready_publications = submission.publications.filter(
        status__in=[
            Publication.Status.READY_FOR_MANUAL_PUBLISH,
            Publication.Status.MANUAL_PUBLISHED_RECORDED,
        ],
        requested_by_principal=request.user,
    )
    form = PublicationProofForm(
        request.POST,
        request.FILES,
        publications=ready_publications,
    )
    if not form.is_valid():
        return render(
            request,
            "dashboard/release_detail.html",
            _release_context(task, request.user, proof_form=form),
            status=400,
        )
    publication = form.cleaned_data["publication"]
    uploaded = form.cleaned_data["proof_file"]
    mime_type = (uploaded.content_type or "application/octet-stream").lower()
    if mime_type not in ALLOWED_PROOF_TYPES:
        messages.error(request, "发布证明仅支持 PNG、JPEG、WebP 或 PDF。")
        return redirect("dashboard:release-detail", task_id=task.pk)
    try:
        proof_sha256, _size = _hash_proof(uploaded)
        if not _valid_proof_signature(uploaded, mime_type):
            raise ValidationError("发布证明的文件内容与所选图片/PDF类型不一致。")
        root = form.cleaned_data["command_id"]
        existing = PublicationEvent.objects.filter(command_id=root).first()
        if existing is not None:
            if (
                existing.publication_id != publication.pk
                or existing.event_type != PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED
                or existing.external_url != form.cleaned_data["external_url"]
                or existing.external_publication_id != form.cleaned_data["external_publication_id"]
                or existing.proof_sha256 != proof_sha256
            ):
                raise ValidationError("该 command_id 已用于不同的发布证明。")
            record_manual_publication_proof(
                publication=publication,
                publisher_principal=request.user,
                command_id=root,
                external_url=existing.external_url,
                external_publication_id=existing.external_publication_id,
                proof_reference=existing.proof_reference,
                proof_sha256=existing.proof_sha256,
            )
        else:
            original_name = Path(uploaded.name or "proof.bin").name
            safe_name = default_storage.get_valid_name(original_name) or "proof.bin"
            requested_name = f"publication-proofs/{publication.pk}/{root.hex}/{safe_name}"
            stored_name = ""
            try:
                stored_name = default_storage.save(requested_name, uploaded)
                record_manual_publication_proof(
                    publication=publication,
                    publisher_principal=request.user,
                    command_id=root,
                    external_url=form.cleaned_data["external_url"],
                    external_publication_id=form.cleaned_data["external_publication_id"],
                    proof_reference=stored_name,
                    proof_sha256=proof_sha256,
                )
            except Exception:
                _delete_proof(stored_name)
                raise
        messages.success(request, "人工发布证明已保存；系统没有执行任何外部发布动作。")
    except ValidationError as error:
        messages.error(request, _validation_text(error))
    return redirect("dashboard:release-detail", task_id=task.pk)


@login_required
@require_POST
def release_done_action(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(Task.objects.select_related("product"), pk=task_id)
    complete_grant = _require_product_grant(
        request.user, task, PermissionGrant.Action.COMPLETE_TASK
    )
    form = CompleteTaskForm(request.POST, state_version=task.state_version)
    if not form.is_valid():
        return render(
            request,
            "dashboard/release_detail.html",
            _release_context(task, request.user, done_form=form),
            status=400,
        )
    try:
        Task.transition(
            task_id=task.pk,
            to_state=Task.State.DONE,
            command_id=form.cleaned_data["command_id"],
            expected_state_version=form.cleaned_data["expected_state_version"],
            actor_principal=request.user,
            acting_role=request.user.role,
            permission_grant=complete_grant,
            recorded_by_principal=request.user,
            reason="Owner/Admin confirmed immutable manual publication proof.",
        )
        messages.success(request, "发布证明已核对，任务已完成。")
        return redirect("dashboard:release-queue")
    except ValidationError as error:
        messages.error(request, _validation_text(error))
    return redirect("dashboard:release-detail", task_id=task.pk)
