from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import F, Prefetch, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.authorization import require_authorization, resolve_authorization
from accounts.models import PermissionGrant, Principal
from contentops.models import ContentAsset, ContentAssetVersion, ReviewDecision, TaskSubmission
from dashboard.forms import (
    AssignmentForm,
    CancelTaskForm,
    DoRForm,
    ResumeDraftForm,
    StartWorkForm,
    TaskCreateForm,
    UploadDoDForm,
    WithdrawSubmissionForm,
    criterion_label,
)
from dashboard.storage_cleanup import StoredObjectWrite
from products.models import ProductProfileVersion
from releasegate.models import Publication
from workflow.models import Task, TaskAssignment, TaskCheckRun, TaskStateEvent


MAX_UPLOAD_BYTES = 100 * 1024 * 1024
ALLOWED_UPLOAD_TYPES = {
    "application/json",
    "application/msword",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/csv",
    "text/markdown",
    "text/plain",
    "video/mp4",
    "video/quicktime",
    "video/webm",
}


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
        return "upload", UploadDoDForm(criteria=task.contract_version.dod_criteria, **common)
    return "", None


def _detail_context(
    task: Task,
    user: Principal,
    *,
    action_kind=None,
    action_form=None,
    cancel_form=None,
    withdraw_form=None,
) -> dict:
    _decorate_task(task)
    if action_kind is None:
        action_kind, action_form = _action_form(task, user)
    return {
        "task": task,
        "action_kind": action_kind,
        "action_form": action_form,
        "submission": task.submissions.order_by("-submission_number").first(),
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
    tasks = list(
        Task.objects.filter(
            Q(current_assignee_principal=request.user) | Q(created_by_principal=request.user)
        )
        .exclude(current_state=Task.State.CANCELLED)
        .select_related("product", "contract_version", "current_assignee_principal")
        .prefetch_related(
            Prefetch(
                "check_runs",
                queryset=TaskCheckRun.objects.filter(check_kind=TaskCheckRun.Kind.DOR).order_by(
                    "-attempt_number"
                ),
                to_attr="dor_runs",
            ),
            Prefetch(
                "check_runs",
                queryset=TaskCheckRun.objects.filter(check_kind=TaskCheckRun.Kind.DOD).order_by(
                    "-attempt_number"
                ),
                to_attr="dod_runs",
            ),
        )
        .distinct()
        .order_by("-updated_at", "title")
    )
    for task in tasks:
        _decorate_task(task)
    active_states = {
        Task.State.READY,
        Task.State.ASSIGNED,
        Task.State.IN_PROGRESS,
        Task.State.HUMAN_REWORK,
    }
    can_create_task = _editable_profiles(request.user).exists()
    # These are permission-filtered work queues, not extra employee roles.
    # Import locally to keep the task workspace independent from the review/
    # release view module during app startup.
    from dashboard.review_views import (
        _allowed_accounts,
        _can_complete,
        _can_review_submission,
    )
    pending_review_count = 0
    for review_task in Task.objects.filter(current_state=Task.State.UNDER_REVIEW).select_related(
        "product"
    ):
        review_submission = review_task.submissions.order_by("-submission_number").first()
        if review_submission is not None and _can_review_submission(
            request.user, review_task, review_submission
        ):
            pending_review_count += 1

    pending_publish_count = 0
    pending_complete_count = 0
    for release_task in Task.objects.filter(current_state=Task.State.APPROVED).select_related(
        "product"
    ):
        latest_submission = release_task.submissions.order_by("-submission_number").first()
        has_manual_publication_proof = bool(
            latest_submission
            and latest_submission.publications.filter(
                status=Publication.Status.MANUAL_PUBLISHED_RECORDED
            ).exists()
        )
        if _allowed_accounts(request.user, release_task).exists() and not has_manual_publication_proof:
            pending_publish_count += 1
        if _can_complete(request.user, release_task):
            pending_complete_count += 1
    return render(
        request,
        "dashboard/home.html",
        {
            "tasks": tasks,
            "task_count": len(tasks),
            "active_task_count": sum(task.current_state in active_states for task in tasks),
            "blocked_task_count": sum(task.current_state == Task.State.BLOCKED for task in tasks),
            "can_create_task": can_create_task,
            "pending_review_count": pending_review_count,
            "pending_publish_count": pending_publish_count,
            "pending_complete_count": pending_complete_count,
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


def _asset_kind(mime_type: str) -> str:
    if mime_type.startswith("image/"):
        return ContentAsset.AssetKind.IMAGE
    if mime_type.startswith("video/"):
        return ContentAsset.AssetKind.VIDEO
    if mime_type.startswith("text/") or mime_type == "application/json":
        return ContentAsset.AssetKind.COPY
    return ContentAsset.AssetKind.DOCUMENT


def _hash_upload(uploaded) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    for chunk in uploaded.chunks():
        byte_size += len(chunk)
        if byte_size > MAX_UPLOAD_BYTES:
            raise ValidationError("交付文件超过 100 MB 上限。")
        digest.update(chunk)
    if byte_size == 0:
        raise ValidationError("交付文件不能为空。")
    uploaded.seek(0)
    return digest.hexdigest(), byte_size


def _criterion_results(form: UploadDoDForm) -> dict[str, str]:
    return {
        criterion["key"]: form.cleaned_data[f"{form.field_prefix}{criterion['key']}"]
        for criterion in form.criteria
    }


def _replayed_submission_result(
    *,
    task: Task,
    user: Principal,
    form: UploadDoDForm,
    mime_type: str,
    content_sha256: str,
    byte_size: int,
) -> str | None:
    """Return the prior result for an exact upload-command replay.

    This lookup happens before storage.save(), so an HTTP retry cannot create a
    duplicate object.  Reusing the same root command for a different payload is
    a hard conflict rather than a request to overwrite immutable facts.
    """

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
    is_exact_replay = all(
        (
            existing.task_id == task.pk,
            existing.expected_task_version == form.cleaned_data["expected_state_version"],
            existing.submitted_by_principal_id == user.pk,
            existing.primary_asset_version.content_sha256 == content_sha256,
            existing.primary_asset_version.byte_size == byte_size,
            existing.primary_asset_version.mime_type == mime_type,
            existing.submission_note == form.cleaned_data["submission_note"],
            actual_criteria == _criterion_results(form),
        )
    )
    if not is_exact_replay:
        raise ValidationError(
            "该 command_id 已用于另一份上传内容或表单；请刷新页面后使用新的命令。"
        )
    return existing.dod_check_run.aggregate_result


def _upload_and_submit(task: Task, user: Principal, grant, form: UploadDoDForm) -> str:
    if task.current_assignee_principal_id != user.pk:
        raise PermissionDenied("ONLY_CURRENT_ASSIGNEE_CAN_SUBMIT")
    uploaded = form.cleaned_data["deliverable"]
    mime_type = (uploaded.content_type or "application/octet-stream").lower()
    if mime_type not in ALLOWED_UPLOAD_TYPES:
        raise ValidationError("不支持该文件类型；请上传文本、图片、PDF、Word 或视频文件。")
    content_sha256, byte_size = _hash_upload(uploaded)
    replayed_result = _replayed_submission_result(
        task=task,
        user=user,
        form=form,
        mime_type=mime_type,
        content_sha256=content_sha256,
        byte_size=byte_size,
    )
    if replayed_result is not None:
        return replayed_result

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

    root_command = form.cleaned_data["command_id"]
    original_name = Path(uploaded.name or "deliverable.bin").name
    safe_name = default_storage.get_valid_name(original_name) or "deliverable.bin"
    requested_name = f"task-deliveries/{task.pk}/{root_command.hex}/{safe_name}"
    stored_write = None
    try:
        stored_name = default_storage.save(requested_name, uploaded)
        stored_write = StoredObjectWrite(
            storage=default_storage,
            stored_name=stored_name,
            is_referenced=lambda name: ContentAssetVersion.objects.filter(
                object_key=name
            ).exists(),
        )
        result = ""
        # ``durable=True`` makes this the outer commit boundary in production,
        # so a successful return cannot later leave an orphan after an unseen
        # outer rollback. Django's TestCase wrapper is explicitly supported.
        with transaction.atomic(durable=True):
            asset = task.content_assets.filter(asset_key="primary-deliverable").first()
            if asset is None:
                asset = ContentAsset.create_idempotent(
                    task=task,
                    asset_key="primary-deliverable",
                    title=f"Primary delivery for {task.title}",
                    asset_kind=_asset_kind(mime_type),
                    description="Primary file uploaded through the controlled task UI.",
                    command_id=_subcommand_id(root_command, "content-asset"),
                    actor_principal=user,
                    acting_role=user.role,
                    permission_grant=grant,
                    recorded_by_principal=user,
                )
            version = ContentAssetVersion.create_next(
                content_asset=asset,
                object_key=stored_name,
                mime_type=mime_type,
                byte_size=byte_size,
                content_sha256=content_sha256,
                metadata={"original_filename": original_name, "source": "dashboard-upload"},
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
                        "content_sha256": content_sha256,
                        "byte_size": byte_size,
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
            result = run.aggregate_result
            stored_write.retain_on_commit()
        return result
    except Exception:
        if stored_write is not None:
            stored_write.cleanup_after_rollback()
        raise


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
        elif action == "upload":
            form = UploadDoDForm(
                request.POST,
                request.FILES,
                criteria=task.contract_version.dod_criteria,
                state_version=task.state_version,
            )
            if not form.is_valid():
                return render(request, "dashboard/task_detail.html", _detail_context(task, request.user, action_kind=action, action_form=form), status=400)
            result = _upload_and_submit(task, request.user, grant, form)
            messages.success(
                request,
                "交付已封存并送入人工审核。" if result == TaskCheckRun.Result.PASS else "文件和本次 DoD 已保留，但仍有阻塞项，尚未送审。",
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
