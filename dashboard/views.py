from __future__ import annotations

import hashlib
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.authorization import require_authorization, resolve_authorization
from accounts.models import PermissionGrant, Principal
from contentops.models import ContentAsset, ContentAssetVersion, ReviewDecision, TaskSubmission
from dashboard.forms import (
    AssignmentForm,
    CancelTaskForm,
    ContentGenerateForm,
    ContentRevisionForm,
    DeliveryDoDForm,
    DoRForm,
    ResumeDraftForm,
    StartWorkForm,
    TaskCreateForm,
    WithdrawSubmissionForm,
    criterion_label,
)
from dailyops.content_generation import (
    generate_task_content_draft,
    revise_task_content_draft,
    validate_inline_content_evidence_manifest,
)
from intelligence.models import TaskCompilationContext
from products.models import ProductProfileVersion
from workflow.models import Task, TaskAssignment, TaskCheckRun, TaskStateEvent

from dashboard.action_center import build_action_center


EXTERNAL_URL_MIME_TYPE = "text/uri-list"
EXTERNAL_URL_METADATA = {"source": "external-url"}


def _subcommand_id(root: uuid.UUID, label: str) -> uuid.UUID:
    return uuid.uuid5(root, label)


def _authorization(user: Principal, task: Task, action: str):
    return resolve_authorization(
        principal=user,
        acting_role=user.role,
        action=action,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=task.product,
    )


def _require_edit(user: Principal, task: Task):
    return require_authorization(
        principal=user,
        acting_role=user.role,
        action=PermissionGrant.Action.EDIT,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=task.product,
    )


def _task_for_user(user: Principal, task_id) -> Task:
    task = get_object_or_404(
        Task.objects.select_related(
            "product", "contract_version", "current_assignee_principal", "created_by_principal"
        ),
        pk=task_id,
    )
    if user.pk in {task.created_by_principal_id, task.current_assignee_principal_id}:
        return task
    # The personal workspace is deliberately narrower than a product-wide
    # grant: knowing a task UUID must not make another employee's task visible.
    raise Http404("Task not found.")


def _editable_profiles(user: Principal):
    if not (
        user.can_authenticate
        and user.principal_type == Principal.PrincipalType.HUMAN_USER
    ):
        return ProductProfileVersion.objects.none()
    profile_ids = [
        profile.pk
        for profile in ProductProfileVersion.objects.filter(
            sealed_at__isnull=False,
            product__product_status="ACTIVE",
            product__current_profile_version_id=F("pk"),
            task_contract_versions__isnull=False,
        ).select_related("product").distinct()
        if resolve_authorization(
            principal=user,
            acting_role=user.role,
            action=PermissionGrant.Action.CREATE_TASK,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=profile.product,
        ).allowed
    ]
    return ProductProfileVersion.objects.filter(pk__in=profile_ids).select_related("product").order_by(
        "product__name", "version_number"
    )


def _require_profile_create(user: Principal, profile: ProductProfileVersion):
    return require_authorization(
        principal=user,
        acting_role=user.role,
        action=PermissionGrant.Action.CREATE_TASK,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=profile.product,
    )


def _eligible_operators(task: Task):
    candidates = Principal.objects.filter(
        principal_type=Principal.PrincipalType.HUMAN_USER,
        principal_status=Principal.PrincipalStatus.ACTIVE,
        is_active=True,
    ).order_by("display_name", "username")
    allowed_ids = [
        candidate.pk
        for candidate in candidates
        if resolve_authorization(
            principal=candidate,
            acting_role=candidate.role,
            action=PermissionGrant.Action.EDIT,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=task.product,
        ).allowed
    ]
    return candidates.filter(pk__in=allowed_ids)


def _decorate_task(task: Task) -> None:
    dor_runs = getattr(task, "dor_runs", None)
    dod_runs = getattr(task, "dod_runs", None)
    if dor_runs is None:
        dor_runs = list(task.check_runs.filter(check_kind=TaskCheckRun.Kind.DOR).order_by("-attempt_number"))
    if dod_runs is None:
        dod_runs = list(task.check_runs.filter(check_kind=TaskCheckRun.Kind.DOD).order_by("-attempt_number"))
    task.latest_dor = dor_runs[0] if dor_runs else None
    task.latest_dod = dod_runs[0] if dod_runs else None
    task.dor_summary = [
        criterion_label(criterion, "Unnamed input") for criterion in task.contract_version.dor_criteria
    ]
    task.dod_summary = [
        criterion_label(criterion, "Unnamed requirement")
        for criterion in task.contract_version.dod_criteria
    ]
    task.release_gate_summary = [
        criterion_label(criterion, "Unnamed check")
        for criterion in task.contract_version.release_gate_criteria
    ]


def _inline_content_versions(task: Task):
    """Return only the latest editable inline version for this task."""

    latest_id = (
        ContentAssetVersion.objects.filter(
            content_asset__task=task,
            content_asset__asset_key="publishable-content",
            representation_kind=ContentAssetVersion.RepresentationKind.INLINE_TEXT,
        )
        .order_by("-version_number", "-created_at", "-id")
        .values_list("pk", flat=True)
        .first()
    )
    if latest_id is None:
        return ContentAssetVersion.objects.none()
    return ContentAssetVersion.objects.select_related("content_asset").filter(pk=latest_id)


def _action_form(task: Task, user: Principal):
    common = {"state_version": task.state_version}
    can_edit = _authorization(user, task, PermissionGrant.Action.EDIT).allowed
    if task.current_state == Task.State.DRAFT and can_edit:
        return "dor", DoRForm(criteria=task.contract_version.dor_criteria, **common)
    if task.current_state == Task.State.BLOCKED and task.blocked_from_state == Task.State.DRAFT and can_edit:
        return "resume", ResumeDraftForm(**common)
    if task.current_state == Task.State.READY and _authorization(
        user, task, PermissionGrant.Action.ASSIGN_TASK
    ).allowed:
        return "assign", AssignmentForm(operators=_eligible_operators(task), **common)
    if task.current_state == Task.State.ASSIGNED and task.current_assignee_principal_id == user.pk and can_edit:
        return "start", StartWorkForm(**common)
    if task.current_state == Task.State.HUMAN_REWORK and task.current_assignee_principal_id == user.pk and can_edit:
        return "resume-work", StartWorkForm(**common)
    if task.current_state == Task.State.IN_PROGRESS and task.current_assignee_principal_id == user.pk and can_edit:
        return "deliver", DeliveryDoDForm(
            criteria=task.contract_version.dod_criteria,
            content_versions=_inline_content_versions(task),
            **common,
        )
    return "", None


def _detail_context(
    task: Task,
    user: Principal,
    *,
    action_kind=None,
    action_form=None,
    cancel_form=None,
    withdraw_form=None,
    generate_form=None,
    revision_form=None,
) -> dict:
    _decorate_task(task)
    if action_kind is None:
        action_kind, action_form = _action_form(task, user)
    compilation_context = (
        TaskCompilationContext.objects.select_related(
            "channel_plan__channel_account",
            "capability_state__account_environment_binding__channel_account",
            "capability_state__account_environment_binding__runtime_environment",
        )
        .filter(task_id=task.pk)
        .first()
    )
    inline_versions = _inline_content_versions(task)
    latest_inline_version = inline_versions.first()
    may_edit_content = bool(
        task.current_state == Task.State.IN_PROGRESS
        and task.current_assignee_principal_id == user.pk
        and _authorization(user, task, PermissionGrant.Action.EDIT).allowed
    )
    return {
        "task": task,
        "compilation_context": compilation_context,
        "channel_plan": compilation_context.channel_plan if compilation_context else None,
        "plan_goal_items": (
            list(compilation_context.channel_plan.goal.items())
            if compilation_context and isinstance(compilation_context.channel_plan.goal, dict)
            else []
        ),
        "action_kind": action_kind,
        "action_form": action_form,
        "submission": task.submissions.order_by("-submission_number").first(),
        "latest_inline_version": latest_inline_version,
        "may_edit_content": may_edit_content,
        "generate_form": generate_form if generate_form is not None else (
            ContentGenerateForm(state_version=task.state_version)
            if may_edit_content and latest_inline_version is None and compilation_context is not None
            else None
        ),
        "revision_form": revision_form if revision_form is not None else (
            ContentRevisionForm(
                source_versions=inline_versions,
                state_version=task.state_version,
                initial={
                    "source_version": latest_inline_version,
                    "inline_content": latest_inline_version.inline_content,
                },
            )
            if may_edit_content and latest_inline_version is not None
            else None
        ),
        "cancel_form": cancel_form if cancel_form is not None else (
            CancelTaskForm(state_version=task.state_version)
            if task.current_state == Task.State.DRAFT
            and _authorization(user, task, PermissionGrant.Action.CANCEL_TASK).allowed
            else None
        ),
        "withdraw_form": withdraw_form if withdraw_form is not None else (
            WithdrawSubmissionForm(state_version=task.state_version)
            if task.current_state == Task.State.UNDER_REVIEW
            and task.current_assignee_principal_id == user.pk
            and _authorization(user, task, PermissionGrant.Action.EDIT).allowed
            else None
        ),
    }


@login_required
def home(request: HttpRequest) -> HttpResponse:
    action_center = build_action_center(request.user)
    tasks = list(action_center.tasks)
    for task in tasks:
        _decorate_task(task)
    can_create_task = _editable_profiles(request.user).exists()
    return render(
        request,
        "dashboard/home.html",
        {
            "tasks": tasks,
            "task_count": len(tasks),
            "blocked_task_count": sum(task.current_state == Task.State.BLOCKED for task in tasks),
            "can_create_task": can_create_task,
            "action_center": action_center,
            # Keep these stable context keys for review/release slice tests and
            # any internal links that already consume them.
            "pending_review_count": action_center.pending_review_count,
            "pending_publish_count": action_center.pending_publish_count,
            "pending_complete_count": action_center.pending_complete_count,
        },
    )


def _require_task_action(user: Principal, task: Task, action: str):
    return require_authorization(
        principal=user,
        acting_role=user.role,
        action=action,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=task.product,
    )


@login_required
def task_create(request: HttpRequest) -> HttpResponse:
    profiles = _editable_profiles(request.user)
    if not profiles.exists():
        raise PermissionDenied("NO_EDITABLE_SEALED_PRODUCT_PROFILE")

    if request.method == "POST":
        raw_profile_id = request.POST.get("product_profile_version")
        try:
            parsed_profile_id = uuid.UUID(raw_profile_id) if raw_profile_id else None
        except (TypeError, ValueError):
            parsed_profile_id = None
        selected_profile = (
            ProductProfileVersion.objects.select_related("product").filter(pk=parsed_profile_id).first()
            if parsed_profile_id
            else None
        )
        if selected_profile is not None:
            # Recheck the exact product on every POST. A stale form must fail
            # closed after revocation, and another editable product cannot be
            # used to smuggle an unauthorized profile through ModelChoiceField.
            _require_profile_create(request.user, selected_profile)
        form = TaskCreateForm(request.POST, profiles=profiles)
        if form.is_valid():
            try:
                task, created = Task.create_draft(
                    task_id=form.cleaned_data["task_id"],
                    command_id=form.cleaned_data["command_id"],
                    product_profile_version_id=form.cleaned_data["product_profile_version"].pk,
                    contract_version_id=form.cleaned_data["contract_version"].pk,
                    title=form.cleaned_data["title"],
                    description=form.cleaned_data["description"],
                    actor_principal=request.user,
                    acting_role=request.user.role,
                )
            except ValidationError as error:
                messages.error(request, _validation_text(error))
                return render(request, "dashboard/task_create.html", {"form": form}, status=409)
            messages.success(
                request,
                "任务已创建为 DRAFT，下一步请填写 DoR。"
                if created
                else "这次请求已经处理过，已返回原任务，没有重复创建。",
            )
            return redirect("dashboard:task-detail", task_id=task.pk)
        return render(request, "dashboard/task_create.html", {"form": form}, status=400)

    return render(
        request,
        "dashboard/task_create.html",
        {"form": TaskCreateForm(profiles=profiles)},
    )


@login_required
def task_detail(request: HttpRequest, task_id) -> HttpResponse:
    task = _task_for_user(request.user, task_id)
    return render(request, "dashboard/task_detail.html", _detail_context(task, request.user))


def _validation_text(error: ValidationError) -> str:
    return " ".join(error.messages) if error.messages else "操作未通过校验，请刷新后重试。"


def _record_dor(task: Task, user: Principal, grant, form: DoRForm) -> str:
    with transaction.atomic():
        run = TaskCheckRun.record_completed(
            task=task,
            check_kind=TaskCheckRun.Kind.DOR,
            results=form.result_rows(evidence={"source": "dashboard"}),
            command_id=_subcommand_id(form.cleaned_data["command_id"], "dor-check"),
            evaluator_principal=user,
            acting_role=user.role,
            permission_grant=grant,
            recorded_by_principal=user,
        )
        target = Task.State.READY if run.aggregate_result == TaskCheckRun.Result.PASS else Task.State.BLOCKED
        Task.transition(
            task_id=task.pk,
            to_state=target,
            command_id=_subcommand_id(form.cleaned_data["command_id"], "dor-transition"),
            expected_state_version=form.cleaned_data["expected_state_version"],
            actor_principal=user,
            acting_role=user.role,
            permission_grant=grant,
            recorded_by_principal=user,
            reason=("DoR passed." if target == Task.State.READY else "One or more DoR inputs are blocked."),
        )
    return target


def _assign_task(task: Task, user: Principal, grant, form: AssignmentForm) -> None:
    assignee = form.cleaned_data["assignee"]
    if not _authorization(assignee, task, PermissionGrant.Action.EDIT).allowed:
        raise PermissionDenied("ASSIGNEE_NO_LONGER_AUTHORIZED")
    with transaction.atomic():
        TaskAssignment.record(
            task=task,
            assignee_principal=assignee,
            command_id=_subcommand_id(form.cleaned_data["command_id"], "assignment"),
            expected_task_version=form.cleaned_data["expected_state_version"],
            assigned_by_principal=user,
            acting_role=user.role,
            permission_grant=grant,
            recorded_by_principal=user,
        )
        Task.transition(
            task_id=task.pk,
            to_state=Task.State.ASSIGNED,
            command_id=_subcommand_id(form.cleaned_data["command_id"], "assigned-transition"),
            expected_state_version=form.cleaned_data["expected_state_version"],
            actor_principal=user,
            acting_role=user.role,
            permission_grant=grant,
            recorded_by_principal=user,
        )


def _start_task(task: Task, user: Principal, grant, form: StartWorkForm) -> None:
    if task.current_assignee_principal_id != user.pk:
        raise PermissionDenied("ONLY_CURRENT_ASSIGNEE_CAN_START")
    Task.transition(
        task_id=task.pk,
        to_state=Task.State.IN_PROGRESS,
        command_id=form.cleaned_data["command_id"],
        expected_state_version=form.cleaned_data["expected_state_version"],
        actor_principal=user,
        acting_role=user.role,
        permission_grant=grant,
        recorded_by_principal=user,
    )


def _criterion_results(form: DeliveryDoDForm) -> dict[str, str]:
    return {
        criterion["key"]: form.cleaned_data[f"{form.field_prefix}{criterion['key']}"]
        for criterion in form.criteria
    }


def _replayed_submission_result(
    *,
    task: Task,
    user: Principal,
    form: DeliveryDoDForm,
    selected_version: ContentAssetVersion | None = None,
    external_url: str = "",
    content_sha256: str = "",
    byte_size: int = 0,
) -> str | None:
    """Return the prior result for an exact link-delivery command replay."""

    submission_command = _subcommand_id(form.cleaned_data["command_id"], "submission")
    existing = (
        TaskSubmission.objects.select_related(
            "dod_check_run",
            "primary_asset_version",
            "submitted_by_principal",
        )
        .filter(command_id=submission_command)
        .first()
    )
    if existing is None:
        return None
    actual_criteria = dict(
        existing.dod_check_run.results.values_list("criterion_key", "result")
    )
    same_delivery = (
        existing.primary_asset_version_id == selected_version.pk
        if selected_version is not None
        else all(
            (
                existing.primary_asset_version.object_key == external_url,
                existing.primary_asset_version.content_sha256 == content_sha256,
                existing.primary_asset_version.byte_size == byte_size,
                existing.primary_asset_version.mime_type == EXTERNAL_URL_MIME_TYPE,
                existing.primary_asset_version.metadata == EXTERNAL_URL_METADATA,
            )
        )
    )
    is_exact_replay = all(
        (
            existing.task_id == task.pk,
            existing.expected_task_version == form.cleaned_data["expected_state_version"],
            existing.submitted_by_principal_id == user.pk,
            same_delivery,
            existing.submission_note == form.cleaned_data["submission_note"],
            actual_criteria == _criterion_results(form),
        )
    )
    if not is_exact_replay:
        raise ValidationError(
            "该 command_id 已用于另一份交付内容或表单；请刷新页面后使用新的命令。"
        )
    return existing.dod_check_run.aggregate_result


def _deliver_and_submit(task: Task, user: Principal, grant, form: DeliveryDoDForm) -> str:
    if task.current_assignee_principal_id != user.pk:
        raise PermissionDenied("ONLY_CURRENT_ASSIGNEE_CAN_SUBMIT")
    delivery_mode = form.cleaned_data["delivery_mode"]
    selected_version = (
        form.cleaned_data["content_version"]
        if delivery_mode == DeliveryDoDForm.DeliveryMode.SYSTEM_CONTENT
        else None
    )
    external_url = form.cleaned_data.get("external_url") or ""
    encoded_url = external_url.encode("utf-8")
    content_sha256 = hashlib.sha256(encoded_url).hexdigest() if external_url else ""
    byte_size = len(encoded_url)
    replayed_result = _replayed_submission_result(
        task=task,
        user=user,
        form=form,
        selected_version=selected_version,
        external_url=external_url,
        content_sha256=content_sha256,
        byte_size=byte_size,
    )
    if replayed_result is not None:
        return replayed_result

    root_command = form.cleaned_data["command_id"]
    with transaction.atomic():
        # Serialize content revision and submission on the Task before touching
        # its asset/version rows. This prevents a stale browser tab from
        # submitting v1 while another request has already saved v2.
        task = Task.objects.select_for_update().get(pk=task.pk)
        if task.current_state != Task.State.IN_PROGRESS:
            raise ValidationError("只有正在执行的任务才能提交交付内容。")
        if task.current_assignee_principal_id != user.pk:
            raise PermissionDenied("ONLY_CURRENT_ASSIGNEE_CAN_SUBMIT")
        if task.state_version != form.cleaned_data["expected_state_version"]:
            raise ValidationError("任务状态已经变化，请刷新页面后再提交。")

        supersedes_submission = task.submissions.order_by("-submission_number").first()
        triggering_review = None
        if supersedes_submission is not None:
            try:
                triggering_review = supersedes_submission.final_review
            except ObjectDoesNotExist:
                was_withdrawn = supersedes_submission.withdrawal_events.filter(
                    event_type=TaskStateEvent.EventType.SUBMISSION_WITHDRAWN,
                    task_id=task.pk,
                ).exists()
                if not was_withdrawn:
                    raise ValidationError(
                        "重新提交前，上一份交付必须已被审核要求修改，或由执行负责人正式撤回。"
                    ) from None
            else:
                if triggering_review.decision != ReviewDecision.Decision.CHANGES_REQUESTED:
                    raise ValidationError("只有明确要求修改的审核结论才能创建返工版本。")

        if selected_version is not None:
            locked_asset = ContentAsset.objects.select_for_update().get(
                pk=selected_version.content_asset_id
            )
            version = ContentAssetVersion.objects.select_related("content_asset").get(
                pk=selected_version.pk,
                content_asset=locked_asset,
            )
            latest = ContentAssetVersion.objects.filter(content_asset=locked_asset).order_by(
                "-version_number"
            ).first()
            if (
                version.content_asset.task_id != task.pk
                or version.representation_kind != ContentAssetVersion.RepresentationKind.INLINE_TEXT
                or latest is None
                or latest.pk != version.pk
            ):
                raise ValidationError("请选择这项任务刚保存的最新系统内内容版本。")
            validate_inline_content_evidence_manifest(
                asset_version=version,
                lock=True,
            )
        else:
            asset = task.content_assets.filter(asset_key="primary-deliverable").first()
            if asset is None:
                asset = ContentAsset.create_idempotent(
                    task=task,
                    asset_key="primary-deliverable",
                    title=f"Primary delivery for {task.title}",
                    asset_kind=ContentAsset.AssetKind.OTHER,
                    description="Primary external delivery link submitted through the task UI.",
                    command_id=_subcommand_id(root_command, "content-asset"),
                    actor_principal=user,
                    acting_role=user.role,
                    permission_grant=grant,
                    recorded_by_principal=user,
                )
            version = ContentAssetVersion.create_next(
                content_asset=asset,
                representation_kind=ContentAssetVersion.RepresentationKind.EXTERNAL_URL,
                object_key=external_url,
                mime_type=EXTERNAL_URL_MIME_TYPE,
                byte_size=byte_size,
                content_sha256=content_sha256,
                metadata=EXTERNAL_URL_METADATA,
                command_id=_subcommand_id(root_command, "content-asset-version"),
                actor_principal=user,
                acting_role=user.role,
                permission_grant=grant,
                recorded_by_principal=user,
            )
        run = TaskCheckRun.record_completed(
            task=task,
            check_kind=TaskCheckRun.Kind.DOD,
            results=form.result_rows(
                evidence={
                    "asset_version_id": str(version.pk),
                    "representation_kind": version.representation_kind,
                    "external_url": external_url or None,
                    "content_sha256": version.content_sha256,
                    "byte_size": version.byte_size,
                }
            ),
            command_id=_subcommand_id(root_command, "dod-check"),
            evaluator_principal=user,
            acting_role=user.role,
            permission_grant=grant,
            recorded_by_principal=user,
        )
        if run.aggregate_result == TaskCheckRun.Result.PASS:
            TaskSubmission.seal(
                task=task,
                dod_check_run=run,
                primary_asset_version=version,
                supersedes_submission=supersedes_submission,
                triggering_review=triggering_review,
                submission_note=form.cleaned_data["submission_note"],
                command_id=_subcommand_id(root_command, "submission"),
                expected_task_version=form.cleaned_data["expected_state_version"],
                actor_principal=user,
                acting_role=user.role,
                permission_grant=grant,
                recorded_by_principal=user,
            )
            Task.transition(
                task_id=task.pk,
                to_state=Task.State.SUBMITTED,
                command_id=_subcommand_id(root_command, "submitted-transition"),
                expected_state_version=form.cleaned_data["expected_state_version"],
                actor_principal=user,
                acting_role=user.role,
                permission_grant=grant,
                recorded_by_principal=user,
            )
            task.refresh_from_db()
            Task.transition(
                task_id=task.pk,
                to_state=Task.State.UNDER_REVIEW,
                command_id=_subcommand_id(root_command, "under-review-transition"),
                expected_state_version=task.state_version,
                actor_principal=user,
                acting_role=user.role,
                permission_grant=grant,
                recorded_by_principal=user,
            )
        return run.aggregate_result


@login_required
@require_POST
def task_action(request: HttpRequest, task_id, action: str) -> HttpResponse:
    task = _task_for_user(request.user, task_id)
    if action == "assign":
        grant = _require_task_action(request.user, task, PermissionGrant.Action.ASSIGN_TASK)
    elif action == "cancel":
        grant = _require_task_action(request.user, task, PermissionGrant.Action.CANCEL_TASK)
    else:
        grant = _require_edit(request.user, task)
    form = None
    try:
        if action == "dor":
            form = DoRForm(
                request.POST,
                criteria=task.contract_version.dor_criteria,
                state_version=task.state_version,
            )
            if form.is_valid():
                target = _record_dor(task, request.user, grant, form)
                messages.success(
                    request,
                    "DoR 已通过，任务可以分配。" if target == Task.State.READY else "DoR 已记录为阻塞；原结果会保留。",
                )
            else:
                return render(
                    request,
                    "dashboard/task_detail.html",
                    _detail_context(task, request.user, action_kind=action, action_form=form),
                    status=400,
                )
        elif action == "resume":
            form = ResumeDraftForm(request.POST, state_version=task.state_version)
            if not form.is_valid():
                return render(request, "dashboard/task_detail.html", _detail_context(task, request.user, action_kind=action, action_form=form), status=400)
            Task.transition(
                task_id=task.pk,
                to_state=Task.State.DRAFT,
                command_id=form.cleaned_data["command_id"],
                expected_state_version=form.cleaned_data["expected_state_version"],
                actor_principal=request.user,
                acting_role=request.user.role,
                permission_grant=grant,
                recorded_by_principal=request.user,
                reason="Inputs updated; return to DRAFT for a new DoR run.",
            )
            messages.success(request, "任务已回到准备阶段，请重新逐项填写 DoR。")
        elif action == "assign":
            form = AssignmentForm(
                request.POST,
                operators=_eligible_operators(task),
                state_version=task.state_version,
            )
            if not form.is_valid():
                return render(request, "dashboard/task_detail.html", _detail_context(task, request.user, action_kind=action, action_form=form), status=400)
            _assign_task(task, request.user, grant, form)
            messages.success(request, "任务已明确分配；只有当前负责人可以开始执行。")
        elif action == "start":
            form = StartWorkForm(request.POST, state_version=task.state_version)
            if not form.is_valid():
                return render(request, "dashboard/task_detail.html", _detail_context(task, request.user, action_kind=action, action_form=form), status=400)
            _start_task(task, request.user, grant, form)
            messages.success(request, "任务已进入执行中。")
        elif action == "resume-work":
            form = StartWorkForm(request.POST, state_version=task.state_version)
            if not form.is_valid():
                return render(request, "dashboard/task_detail.html", _detail_context(task, request.user, action_kind=action, action_form=form), status=400)
            _start_task(task, request.user, grant, form)
            messages.success(request, "返工任务已恢复制作；请完成新版本后重新填写 DoD。")
        elif action == "generate-content":
            form = ContentGenerateForm(request.POST, state_version=task.state_version)
            if not form.is_valid():
                return render(
                    request,
                    "dashboard/task_detail.html",
                    _detail_context(task, request.user, generate_form=form),
                    status=400,
                )
            result = generate_task_content_draft(
                task=task,
                command_id=form.cleaned_data["command_id"],
                principal=request.user,
                acting_role=request.user.role,
                permission_grant=grant,
            )
            messages.success(
                request,
                "完整内容草稿已在本机离线生成。请先阅读和修改，再选择该版本送审。"
                if result.created
                else "这次生成请求已经处理过，已返回原内容版本。",
            )
        elif action == "revise-content":
            source_versions = _inline_content_versions(task)
            form = ContentRevisionForm(
                request.POST,
                source_versions=source_versions,
                state_version=task.state_version,
            )
            if not form.is_valid():
                return render(
                    request,
                    "dashboard/task_detail.html",
                    _detail_context(task, request.user, revision_form=form),
                    status=400,
                )
            result = revise_task_content_draft(
                task=task,
                source_version=form.cleaned_data["source_version"],
                inline_content=form.cleaned_data["inline_content"],
                command_id=form.cleaned_data["command_id"],
                principal=request.user,
                acting_role=request.user.role,
                permission_grant=grant,
            )
            messages.success(
                request,
                f"修改已另存为内容 v{result.asset_version.version_number}；旧版本仍保持不变。",
            )
        elif action == "deliver":
            form = DeliveryDoDForm(
                request.POST,
                criteria=task.contract_version.dod_criteria,
                content_versions=_inline_content_versions(task),
                state_version=task.state_version,
            )
            if not form.is_valid():
                return render(request, "dashboard/task_detail.html", _detail_context(task, request.user, action_kind=action, action_form=form), status=400)
            result = _deliver_and_submit(task, request.user, grant, form)
            messages.success(
                request,
                "交付内容已封存并送入人工审核。" if result == TaskCheckRun.Result.PASS else "交付内容和本次检查已保留，但仍有阻塞项，尚未送审。",
            )
        elif action == "cancel":
            form = CancelTaskForm(request.POST, state_version=task.state_version)
            if not form.is_valid():
                return render(
                    request,
                    "dashboard/task_detail.html",
                    _detail_context(task, request.user, cancel_form=form),
                    status=400,
                )
            Task.transition(
                task_id=task.pk,
                to_state=Task.State.CANCELLED,
                command_id=form.cleaned_data["command_id"],
                expected_state_version=form.cleaned_data["expected_state_version"],
                actor_principal=request.user,
                acting_role=request.user.role,
                permission_grant=grant,
                recorded_by_principal=request.user,
                reason=form.cleaned_data["reason"],
            )
            messages.success(request, "草稿已取消并从 Today 隐藏；历史记录仍然保留。")
            return redirect("dashboard:home")
        elif action == "withdraw":
            form = WithdrawSubmissionForm(request.POST, state_version=task.state_version)
            if not form.is_valid():
                return render(
                    request,
                    "dashboard/task_detail.html",
                    _detail_context(task, request.user, withdraw_form=form),
                    status=400,
                )
            submission = task.submissions.order_by("-submission_number").first()
            if submission is None:
                raise ValidationError("没有可撤回的已封存交付版本。")
            Task.withdraw_submission(
                task_id=task.pk,
                submission_id=submission.pk,
                command_id=form.cleaned_data["command_id"],
                expected_state_version=form.cleaned_data["expected_state_version"],
                actor_principal=request.user,
                acting_role=request.user.role,
                permission_grant=grant,
                recorded_by_principal=request.user,
                reason=form.cleaned_data["reason"],
            )
            messages.success(request, "交付已撤回。旧版本仍保留，请修改后重新提交新版本。")
        else:
            raise Http404("Unknown task action.")
    except ValidationError as error:
        messages.error(request, _validation_text(error))
    return redirect("dashboard:task-detail", task_id=task.pk)
