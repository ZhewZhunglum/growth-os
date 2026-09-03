from __future__ import annotations

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import transaction
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.authorization import require_authorization, resolve_authorization
from accounts.models import PermissionGrant, Principal
from contentops.models import ReviewDecision, TaskSubmission
from dashboard.review_forms import (
    CompleteTaskForm,
    PublicationProofForm,
    ReleaseGateForm,
    ReturnToInlineContentForm,
    ReviewDecisionForm,
    StopPublicationForm,
)
from integrations.publishing import PublicationMode, get_publication_runtime
from intelligence.models import TaskCompilationContext
from releasegate.models import ChannelAccount, Publication, PublicationEvent, RuntimeEnvironment
from releasegate.publishing import (
    dispatch_confirmed_publication,
    prepare_human_publication_confirmation,
)
from releasegate.services import orchestrate_v1_release_gate
from workflow.models import Task


def _is_link_delivery(asset_version) -> bool:
    metadata = asset_version.metadata
    return bool(
        asset_version.mime_type == "text/uri-list"
        and isinstance(metadata, dict)
        and metadata.get("source") == "external-url"
    )


def _is_inline_delivery(asset_version) -> bool:
    return bool(
        asset_version.representation_kind == "INLINE_TEXT"
        and asset_version.inline_content.strip()
    )


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
    candidates = ChannelAccount.objects.filter(status=ChannelAccount.Status.ACTIVE)
    compilation_context = (
        TaskCompilationContext.objects.select_related("channel_plan__channel_account")
        .filter(task=task)
        .first()
    )
    if compilation_context is not None:
        # A compiled Daily Operations task is sealed to one exact account.
        # A PUBLISH grant for another account must never make this task appear
        # in the queue or let the user choose a different destination.
        candidates = candidates.filter(
            pk=compilation_context.channel_plan.channel_account_id
        )
    account_ids = [
        account.pk
        for account in candidates.order_by("platform_code", "account_code")
        if _publish_decision(user, task, account).allowed
    ]
    return ChannelAccount.objects.filter(pk__in=account_ids).order_by("platform_code", "account_code")


def _allowed_environments(accounts, *, task: Task | None = None):
    if task is not None:
        compilation_context = (
            TaskCompilationContext.objects.select_related(
                "channel_plan__channel_account",
                "capability_state__account_environment_binding__runtime_environment",
            )
            .filter(task=task)
            .first()
        )
        if compilation_context is not None:
            exact_account_id = compilation_context.channel_plan.channel_account_id
            if not exact_account_id or not accounts.filter(pk=exact_account_id).exists():
                return RuntimeEnvironment.objects.none()
            exact_environment_id = (
                compilation_context.capability_state.account_environment_binding.runtime_environment_id
            )
            # A compiled Daily Operations task is sealed to the exact account
            # *and* environment represented by its capability snapshot.  A
            # second active binding must never make production/staging
            # interchangeable in the release form.
            return RuntimeEnvironment.objects.filter(
                pk=exact_environment_id,
                status=RuntimeEnvironment.Status.ACTIVE,
            )
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


def _can_manage_approved_publication(user: Principal, task: Task) -> bool:
    """Allow an authorized Owner/Admin to open the page to stop or rework it."""

    if user.role not in {
        Principal.Role.OWNER,
        Principal.Role.OPERATIONS_ADMIN,
    }:
        return False
    return bool(
        _product_decision(user, task, PermissionGrant.Action.EDIT).allowed
        or _product_decision(user, task, PermissionGrant.Action.CANCEL_TASK).allowed
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

    is_owner_self_approval = bool(
        submission.submitted_by_principal_id == user.pk
        and user.role == Principal.Role.OWNER
    )
    return bool(
        (
            submission.submitted_by_principal_id != user.pk
            or is_owner_self_approval
        )
        and _product_decision(user, task, PermissionGrant.Action.REVIEW).allowed
        and _product_decision(user, task, PermissionGrant.Action.EDIT).allowed
    )


def _can_view_asset_version(
    user: Principal,
    task: Task,
    submission: TaskSubmission,
) -> bool:
    """Authorize a read of the exact immutable primary asset version."""

    if submission.primary_asset_version_id is None:
        return False
    if ReviewDecision.objects.filter(
        submission=submission,
        reviewer_principal=user,
    ).exists() and _product_decision(
        user, task, PermissionGrant.Action.REVIEW
    ).allowed:
        return True
    latest_submission = task.submissions.order_by("-submission_number").first()
    if latest_submission is None or latest_submission.pk != submission.pk:
        return False
    if task.current_state == Task.State.UNDER_REVIEW and _can_review_submission(
        user, task, submission
    ):
        return True
    if user.pk in {
        task.created_by_principal_id,
        task.current_assignee_principal_id,
    } and _product_decision(user, task, PermissionGrant.Action.EDIT).allowed:
        return True
    if task.current_state in {
        Task.State.APPROVED,
        Task.State.DONE,
    } and _product_decision(
        user, task, PermissionGrant.Action.COMPLETE_TASK
    ).allowed:
        return True
    if task.current_state == Task.State.APPROVED:
        # A publisher with independent product VIEW access must be able to
        # inspect the exact approved content before creating a release gate.
        # Requiring an existing gate here creates a circular flow (gate first,
        # content second), while PUBLISH alone must not imply content access.
        if (
            _product_decision(user, task, PermissionGrant.Action.VIEW).allowed
            and _allowed_accounts(user, task).exists()
        ):
            return True
        publication = submission.publications.select_related(
            "current_gate__channel_account"
        ).filter(
            requested_by_principal=user,
            current_gate__isnull=False,
        ).order_by("-created_at", "-id").first()
        if publication and _publish_decision(
            user, task, publication.current_gate.channel_account
        ).allowed:
            return True
    return False


def _can_view_publication_proof_event(
    user: Principal,
    event: PublicationEvent,
) -> bool:
    task = event.publication.submission.task
    current_gate = event.publication.current_gate
    may_complete = _product_decision(
        user,
        task,
        PermissionGrant.Action.COMPLETE_TASK,
    ).allowed
    may_publish = bool(
        current_gate
        and event.publication.requested_by_principal_id == user.pk
        and _publish_decision(
            user,
            task,
            current_gate.channel_account,
        ).allowed
    )
    return may_complete or may_publish


def _review_context(task: Task, user: Principal, *, form=None):
    submission = _latest_submission(task)
    asset_version = submission.primary_asset_version
    owner_self_approval = bool(
        submission.submitted_by_principal_id == user.pk
        and user.role == Principal.Role.OWNER
    )
    return {
        "task": task,
        "submission": submission,
        "asset_version": asset_version,
        "is_link_delivery": _is_link_delivery(asset_version),
        "is_inline_delivery": _is_inline_delivery(asset_version),
        "review_form": form
        or ReviewDecisionForm(
            state_version=task.state_version,
            owner_self_approval=owner_self_approval,
        ),
        "owner_self_approval": owner_self_approval,
    }


def _release_context(task: Task, user: Principal, *, gate_form=None, proof_form=None, done_form=None):
    submission = _latest_submission(task)
    accounts = _allowed_accounts(user, task)
    environments = _allowed_environments(accounts, task=task)
    publications = submission.publications.select_related(
        "current_gate__channel_account",
        "current_gate__runtime_environment",
        "requested_by_principal",
    ).prefetch_related("events").order_by("-created_at", "-id")
    current_release_publication = publications.filter(
        requested_by_principal=user,
    ).first()
    ready_publications = publications.filter(
        status=Publication.Status.READY_FOR_MANUAL_PUBLISH,
        requested_by_principal=user,
    )
    initial_publication = ready_publications.first()
    can_publish = accounts.exists()
    publication_list = list(publications)
    asset_version = submission.primary_asset_version
    can_view_asset = _can_view_asset_version(user, task, submission)
    is_link_delivery = _is_link_delivery(asset_version)
    is_inline_delivery = _is_inline_delivery(asset_version)
    release_content_ready = can_view_asset and is_inline_delivery
    is_owner_or_admin = user.role in {
        Principal.Role.OWNER,
        Principal.Role.OPERATIONS_ADMIN,
    }
    can_stop_publication = bool(
        is_owner_or_admin
        and _product_decision(user, task, PermissionGrant.Action.CANCEL_TASK).allowed
    )
    has_exact_approved_review = ReviewDecision.objects.filter(
        submission=submission,
        decision=ReviewDecision.Decision.APPROVED,
    ).exists()
    can_return_to_rework = bool(
        is_owner_or_admin
        and is_link_delivery
        and has_exact_approved_review
        and _product_decision(user, task, PermissionGrant.Action.EDIT).allowed
    )
    current_gate = (
        current_release_publication.current_gate
        if current_release_publication is not None
        else None
    )
    current_gate_blockers = current_gate.current_blockers() if current_gate else []
    current_gate_is_valid = bool(current_gate and not current_gate_blockers)
    proof_recorded = bool(
        current_release_publication
        and current_release_publication.status
        == Publication.Status.MANUAL_PUBLISHED_RECORDED
    )
    visible_proof_form = None
    if release_content_ready and current_gate_is_valid and initial_publication and can_publish:
        visible_proof_form = proof_form or PublicationProofForm(
            publications=ready_publications,
            initial_publication=initial_publication,
        )
    readable_proof_event_ids = {
        event.pk
        for publication in publication_list
        for event in publication.events.all()
        if (event.external_url or event.external_publication_id)
        and _can_view_publication_proof_event(user, event)
    }
    return {
        "task": task,
        "submission": submission,
        "publications": publication_list,
        "gate_form": gate_form or ReleaseGateForm(
            accounts=accounts,
            environments=environments,
            state_version=task.state_version,
        ),
        "proof_form": visible_proof_form,
        "done_form": done_form or (
            CompleteTaskForm(state_version=task.state_version) if _can_complete(user, task) else None
        ),
        "can_publish": can_publish,
        "can_view_asset": can_view_asset,
        "is_link_delivery": is_link_delivery,
        "is_inline_delivery": is_inline_delivery,
        "release_content_ready": release_content_ready,
        "current_release_publication": current_release_publication,
        "current_gate": current_gate,
        "current_gate_blockers": current_gate_blockers,
        "current_gate_is_valid": current_gate_is_valid,
        "proof_recorded": proof_recorded,
        "can_stop_publication": can_stop_publication,
        "stop_form": (
            StopPublicationForm(state_version=task.state_version)
            if can_stop_publication
            else None
        ),
        "can_return_to_rework": can_return_to_rework,
        "rework_form": (
            ReturnToInlineContentForm(state_version=task.state_version)
            if can_return_to_rework
            else None
        ),
        "requires_inline_rework": is_link_delivery,
        "readable_proof_event_ids": readable_proof_event_ids,
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
            task.owner_self_approval = bool(
                submission.submitted_by_principal_id == request.user.pk
                and request.user.role == Principal.Role.OWNER
            )
            tasks.append(task)
    completed_reviews = (
        ReviewDecision.objects.filter(reviewer_principal=request.user)
        .select_related(
            "submission__task__product",
            "submission__primary_asset_version__content_asset",
        )
        .order_by("-decided_at", "-id")
    )
    # The review queue stays strictly actionable. An Owner may give the final
    # audited approval to their own submission; other submitters see a separate
    # read-only handoff summary so their submission never appears to disappear.
    from dashboard.action_center import build_action_center

    waiting_reviews = build_action_center(request.user).waiting_items
    review_paginator = Paginator(tasks, 10)
    review_page = review_paginator.get_page(request.GET.get("review_page", 1))
    history_paginator = Paginator(completed_reviews, 10)
    history_page = history_paginator.get_page(request.GET.get("history_page", 1))
    return render(
        request,
        "dashboard/review_queue.html",
        {
            "tasks": review_page.object_list,
            "review_page_obj": review_page,
            "completed_reviews": history_page.object_list,
            "history_page_obj": history_page,
            "waiting_reviews": waiting_reviews,
        },
    )


@login_required
def review_detail(request: HttpRequest, task_id) -> HttpResponse:
    task = get_object_or_404(
        Task.objects.select_related("product", "contract_version", "current_assignee_principal"),
        pk=task_id,
        current_state=Task.State.UNDER_REVIEW,
    )
    submission = _latest_submission(task)
    # Keep the GET route aligned with the queue and POST route: a reviewer
    # needs both REVIEW (to decide) and EDIT (to project the task state). The
    # only self-approval exception is the narrow, audited Owner path encoded
    # in the shared helper.
    if not _can_review_submission(request.user, task, submission):
        if (
            submission.submitted_by_principal_id == request.user.pk
            and request.user.role != Principal.Role.OWNER
        ):
            raise PermissionDenied("SUBMITTER_CANNOT_REVIEW_OWN_SUBMISSION")
        raise PermissionDenied("CURRENT_REVIEW_AUTHORIZATION_REQUIRED")
    return render(
        request,
        "dashboard/review_detail.html",
        _review_context(task, request.user),
    )


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
            "is_link_delivery": _is_link_delivery(
                review.submission.primary_asset_version
            ),
            "is_inline_delivery": _is_inline_delivery(
                review.submission.primary_asset_version
            ),
            "can_view_asset": _can_view_asset_version(
                request.user,
                review.submission.task,
                review.submission,
            ),
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
    submission = _latest_submission(task)
    owner_self_approval = bool(
        submission.submitted_by_principal_id == request.user.pk
        and request.user.role == Principal.Role.OWNER
    )
    if (
        submission.submitted_by_principal_id == request.user.pk
        and not owner_self_approval
    ):
        raise PermissionDenied("SUBMITTER_CANNOT_REVIEW_OWN_SUBMISSION")
    form = ReviewDecisionForm(
        request.POST,
        state_version=task.state_version,
        owner_self_approval=owner_self_approval,
    )
    if not form.is_valid():
        return render(
            request,
            "dashboard/review_detail.html",
            _review_context(task, request.user, form=form),
            status=400,
        )
    decision = form.cleaned_data["decision"]
    if owner_self_approval and decision != ReviewDecision.Decision.APPROVED:
        raise PermissionDenied("OWNER_SELF_APPROVAL_CAN_ONLY_APPROVE")
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
                owner_edit_grant=(edit_grant if owner_self_approval else None),
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
@require_POST
def review_batch_action(request: HttpRequest) -> JsonResponse:
    """Batch approve reviews. Each task is processed independently; failures are skipped and reported."""
    task_ids = request.POST.getlist("task_ids")
    if not task_ids:
        return JsonResponse({"ok": False, "message": "未选择任何任务。"}, status=400)
    approved = []
    failed = []
    for task_id in task_ids:
        try:
            task = Task.objects.select_related("product", "contract_version").get(pk=task_id)
            if task.current_state != Task.State.UNDER_REVIEW:
                failed.append({"id": str(task_id), "reason": "任务不在审核中状态。"})
                continue
            review_grant = _require_product_grant(request.user, task, PermissionGrant.Action.REVIEW)
            edit_grant = _require_product_grant(request.user, task, PermissionGrant.Action.EDIT)
            submission = _latest_submission(task)
            owner_self_approval = bool(
                submission.submitted_by_principal_id == request.user.pk
                and request.user.role == Principal.Role.OWNER
            )
            if submission.submitted_by_principal_id == request.user.pk and not owner_self_approval:
                failed.append({"id": str(task_id), "reason": "不能审核自己提交的内容。"})
                continue
            root = uuid.uuid4()
            with transaction.atomic():
                ReviewDecision.record_final(
                    submission=submission,
                    decision=ReviewDecision.Decision.APPROVED,
                    rationale="批量审核通过。",
                    command_id=_subcommand(root, "review-decision"),
                    expected_task_version=task.state_version,
                    reviewer_principal=request.user,
                    acting_role=request.user.role,
                    permission_grant=review_grant,
                    recorded_by_principal=request.user,
                )
                Task.transition(
                    task_id=task.pk,
                    to_state=Task.State.APPROVED,
                    command_id=_subcommand(root, "review-transition"),
                    expected_task_version=task.state_version,
                    actor_principal=request.user,
                    acting_role=request.user.role,
                    permission_grant=edit_grant,
                    recorded_by_principal=request.user,
                    reason="Batch human review approved.",
                )
            approved.append(str(task_id))
        except (Task.DoesNotExist, PermissionDenied, ValidationError) as error:
            failed.append({"id": str(task_id), "reason": str(error) if hasattr(error, "messages") else "处理失败。"})
        except Exception:
            failed.append({"id": str(task_id), "reason": "未知错误。"})
    return JsonResponse({
        "ok": True,
        "approved_count": len(approved),
        "failed_count": len(failed),
        "failed": failed,
        "redirect": "/review/",
    })


@login_required
def release_queue(request: HttpRequest) -> HttpResponse:
    tasks = []
    for task in Task.objects.filter(current_state=Task.State.APPROVED).select_related(
        "product", "contract_version", "current_assignee_principal"
    ).order_by("updated_at", "title"):
        if (
            _allowed_accounts(request.user, task).exists()
            or _can_complete(request.user, task)
            or _can_manage_approved_publication(request.user, task)
        ):
            tasks.append(task)
    paginator = Paginator(tasks, 10)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    return render(request, "dashboard/release_queue.html", {"tasks": page_obj.object_list, "page_obj": page_obj})


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
        and not _can_manage_approved_publication(request.user, task)
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
    environments = _allowed_environments(accounts, task=task)
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
            messages.success(request, "门禁已通过。请选择发布方式并完成最终人工确认；未配置的 API/浏览器路径会直接拒绝。")
        else:
            messages.error(request, "门禁未通过，系统没有发布任何内容。请先处理页面显示的阻塞原因。")
    except ValidationError as error:
        messages.error(request, _validation_text(error))
    return redirect("dashboard:release-detail", task_id=task.pk)


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
    # return its existing immutable URL/content-ID proof without creating another event.
    ready_publications = submission.publications.filter(
        status__in=[
            Publication.Status.READY_FOR_MANUAL_PUBLISH,
            Publication.Status.MANUAL_PUBLISHED_RECORDED,
        ],
        requested_by_principal=request.user,
    )
    form = PublicationProofForm(
        request.POST,
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
    try:
        root_command = form.cleaned_data["command_id"]
        mode = PublicationMode(form.cleaned_data["mode"])
        confirmation = prepare_human_publication_confirmation(
            publication=publication,
            publisher_principal=request.user,
            mode=mode,
            confirmation_id=_subcommand(root_command, "human-publish-confirmation"),
            confirmed=form.cleaned_data["confirmed"],
        )
        runtime = None
        if mode is not PublicationMode.MANUAL:
            try:
                runtime = get_publication_runtime()
            except ImproperlyConfigured as error:
                raise ValidationError(
                    {"mode": "受控发布运行层配置无效；本次没有执行发布。"}
                ) from error
        result = dispatch_confirmed_publication(
            publication=publication,
            publisher_principal=request.user,
            confirmation=confirmation,
            command_id=root_command,
            runtime=runtime,
            manual_external_url=form.cleaned_data["external_url"],
            manual_external_publication_id=form.cleaned_data["external_publication_id"],
        )
        if result.mode is PublicationMode.MANUAL:
            messages.success(request, "人工发布确认和外部网址/内容 ID 已保存。")
        else:
            messages.success(request, "受控发布已完成，并保存了不可变发布证明。")
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
