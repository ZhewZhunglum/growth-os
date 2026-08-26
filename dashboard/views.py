from __future__ import annotations

import hashlib
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.authorization import require_authorization, resolve_authorization
from accounts.models import PermissionGrant, Principal
from contentops.models import (
    DAILY_OPERATIONS_MIN_INLINE_CHARS,
    ContentAsset,
    ContentAssetVersion,
    ReviewDecision,
    TaskSubmission,
)
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


def _can_manage_assignment(user: Principal, task: Task) -> bool:
    if (
        user.role == Principal.Role.OPERATIONS_ADMIN
        and task.current_assignee_principal_id is not None
        and task.current_assignee_principal.role != Principal.Role.OPERATOR
    ):
        return False
    return bool(
        user.role in {Principal.Role.OWNER, Principal.Role.OPERATIONS_ADMIN}
        and task.current_state in {
            Task.State.READY,
            Task.State.ASSIGNED,
            Task.State.IN_PROGRESS,
        }
        and not task.submissions.exists()
        and _authorization(user, task, PermissionGrant.Action.ASSIGN_TASK).allowed
    )


TASK_MANAGEMENT_CANCELLABLE_STATES = {
    Task.State.DRAFT,
    Task.State.BLOCKED,
    Task.State.READY,
    Task.State.ASSIGNED,
    Task.State.IN_PROGRESS,
    Task.State.HUMAN_REWORK,
    Task.State.UNDER_REVIEW,
}


def _can_cancel_task(user: Principal, task: Task) -> bool:
    if task.current_state not in TASK_MANAGEMENT_CANCELLABLE_STATES:
        return False
    if not _authorization(user, task, PermissionGrant.Action.CANCEL_TASK).allowed:
        return False
    is_owner_or_admin = user.role in {
        Principal.Role.OWNER,
        Principal.Role.OPERATIONS_ADMIN,
    }
    if task.current_state == Task.State.UNDER_REVIEW:
        submission = task.submissions.order_by("-submission_number").first()
        if submission is None or ReviewDecision.objects.filter(submission=submission).exists():
            return False
        if TaskStateEvent.objects.filter(
            event_type__in={
                TaskStateEvent.EventType.SUBMISSION_WITHDRAWN,
                TaskStateEvent.EventType.SUBMISSION_ABANDONED,
            },
            submission=submission,
        ).exists():
            return False
        return bool(
            submission.submitted_by_principal_id == user.pk or is_owner_or_admin
        )
    creator_can_cancel_unassigned = (
        task.current_assignee_principal_id is None
        and task.created_by_principal_id == user.pk
        and task.current_state
        in {Task.State.DRAFT, Task.State.READY, Task.State.BLOCKED}
    )
    return bool(
        task.current_assignee_principal_id == user.pk
        or creator_can_cancel_unassigned
        or is_owner_or_admin
    )


def _task_management_kind(user: Principal, task: Task) -> str:
    if task.current_state in {Task.State.DONE, Task.State.CANCELLED}:
        return "read-only"
    if task.current_state == Task.State.APPROVED:
        if (
            user.role in {Principal.Role.OWNER, Principal.Role.OPERATIONS_ADMIN}
            and _authorization(user, task, PermissionGrant.Action.CANCEL_TASK).allowed
        ):
            return "stop-publication"
        return "read-only"
    if not _can_cancel_task(user, task):
        return ""
    if task.current_state == Task.State.DRAFT:
        return "delete-draft"
    if task.current_state == Task.State.UNDER_REVIEW:
        return "withdraw-abandon"
    return "abandon"


def _task_for_user(user: Principal, task_id) -> Task:
    task = get_object_or_404(
        Task.objects.select_related(
            "product", "contract_version", "current_assignee_principal", "created_by_principal"
        ),
        pk=task_id,
    )
    if user.pk in {task.created_by_principal_id, task.current_assignee_principal_id}:
        return task
    if _can_cancel_task(user, task):
        return task
    if _can_manage_assignment(user, task):
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


def _eligible_operators(task: Task, manager: Principal | None = None, *, exclude_current: bool = False):
    candidates = Principal.objects.filter(
        principal_type=Principal.PrincipalType.HUMAN_USER,
        principal_status=Principal.PrincipalStatus.ACTIVE,
        is_active=True,
    ).order_by("display_name", "username")
    if manager is not None and manager.role == Principal.Role.OPERATIONS_ADMIN:
        candidates = candidates.filter(role=Principal.Role.OPERATOR)
    if exclude_current and task.current_assignee_principal_id:
        candidates = candidates.exclude(pk=task.current_assignee_principal_id)
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


def _principal_turn_label(principal: Principal) -> str:
    name = principal.display_name or principal.username
    role = {
        Principal.Role.OWNER: "Owner",
        Principal.Role.OPERATIONS_ADMIN: "Admin",
        Principal.Role.OPERATOR: "Operator",
    }.get(principal.role, principal.role)
    return f"{name}（{role}）"


def _authorized_product_people(task: Task, action: str, *, exclude_principal_id=None) -> list[Principal]:
    people = Principal.objects.filter(
        principal_type=Principal.PrincipalType.HUMAN_USER,
        principal_status=Principal.PrincipalStatus.ACTIVE,
        is_active=True,
    ).order_by("display_name", "username")
    return [
        person
        for person in people
        if person.pk != exclude_principal_id
        and _authorization(person, task, action).allowed
    ]


def _turn_people_label(people: list[Principal], fallback_zh: str, fallback_en: str) -> tuple[str, str]:
    if not people:
        return fallback_zh, fallback_en
    labels = "、".join(_principal_turn_label(person) for person in people[:3])
    if len(people) > 3:
        labels += f" 等 {len(people)} 人"
    return labels, labels


def _current_task_turn(task: Task, compilation_context=None) -> dict[str, str]:
    assignee = task.current_assignee_principal
    creator = task.created_by_principal
    if task.current_state == Task.State.DRAFT:
        who = _principal_turn_label(creator)
        return {
            "who_zh": who,
            "who_en": who,
            "action_zh": "补齐资料并完成开始前检查",
            "action_en": "Complete the information and readiness check",
            "note_zh": "确认资料齐全后，这份任务才会进入分配阶段。",
            "note_en": "The task can be assigned only after its information is ready.",
        }
    if task.current_state == Task.State.READY:
        people = [
            person
            for person in _authorized_product_people(task, PermissionGrant.Action.ASSIGN_TASK)
            if person.role in {Principal.Role.OWNER, Principal.Role.OPERATIONS_ADMIN}
        ]
        who_zh, who_en = _turn_people_label(people, "尚未配置分配负责人", "No assignment manager configured")
        return {
            "who_zh": who_zh,
            "who_en": who_en,
            "action_zh": "选择一位执行人",
            "action_en": "Choose an assignee",
            "note_zh": "只有拥有当前有效分配权限的 Owner 或 Admin 可以操作。",
            "note_en": "Only an Owner or Admin with live assignment permission can act.",
        }
    if task.current_state in {Task.State.ASSIGNED, Task.State.IN_PROGRESS, Task.State.HUMAN_REWORK}:
        who = _principal_turn_label(assignee) if assignee else "尚未分配"
        action_zh = {
            Task.State.ASSIGNED: "确认开工",
            Task.State.IN_PROGRESS: "完成内容并送审",
            Task.State.HUMAN_REWORK: "按审核意见修改后重新送审",
        }[task.current_state]
        action_en = {
            Task.State.ASSIGNED: "Start the task",
            Task.State.IN_PROGRESS: "Finish the content and submit it",
            Task.State.HUMAN_REWORK: "Revise and resubmit after review feedback",
        }[task.current_state]
        return {
            "who_zh": who,
            "who_en": who,
            "action_zh": action_zh,
            "action_en": action_en,
            "note_zh": "当前负责人完成后，系统会把下一步送到正确的审核或发布队列。",
            "note_en": "When the assignee finishes, the next step moves to the correct review or publishing queue.",
        }
    if task.current_state in {Task.State.SUBMITTED, Task.State.UNDER_REVIEW}:
        submission = task.submissions.select_related("submitted_by_principal").order_by(
            "-submission_number"
        ).first()
        owner_self_approval = bool(
            submission
            and submission.submitted_by_principal.role == Principal.Role.OWNER
        )
        if submission is None:
            people = []
        else:
            # Import lazily so the task-detail projection uses the exact same
            # REVIEW + EDIT and narrow Owner self-approval rule as the review
            # queue/detail/action endpoints without introducing an import
            # cycle during app initialization.
            from dashboard.review_views import _can_review_submission

            people = [
                person
                for person in Principal.objects.filter(
                    principal_type=Principal.PrincipalType.HUMAN_USER,
                    principal_status=Principal.PrincipalStatus.ACTIVE,
                    is_active=True,
                ).order_by("display_name", "username")
                if _can_review_submission(person, task, submission)
            ]
        who_zh, who_en = _turn_people_label(
            people,
            "尚未配置可审核账号，请 Owner 处理",
            "No eligible reviewer is configured; ask the Owner to handle it",
        )
        return {
            "who_zh": who_zh,
            "who_en": who_en,
            "action_zh": "审核这次提交",
            "action_en": "Review this submission",
            "note_zh": (
                "Owner 可对自己提交的内容做最终批准并留下审计；Admin/Operator 提交后仍需由另一位有权限的账号审核。"
                if owner_self_approval
                else "Admin/Operator 提交后不能自审；请由另一位拥有审核权限的账号处理。"
            ),
            "note_en": (
                "An Owner may give final approval to their own submission with an audit record; Admin and Operator submissions still require another authorized reviewer."
                if owner_self_approval
                else "Admin and Operator submitters cannot self-review; another authorized account must handle the review."
            ),
        }
    if task.current_state == Task.State.APPROVED:
        account = (
            compilation_context.channel_plan.channel_account
            if compilation_context and compilation_context.channel_plan.channel_account_id
            else None
        )
        people: list[Principal] = []
        if account is not None:
            for person in Principal.objects.filter(
                principal_type=Principal.PrincipalType.HUMAN_USER,
                principal_status=Principal.PrincipalStatus.ACTIVE,
                is_active=True,
            ).order_by("display_name", "username"):
                decision = resolve_authorization(
                    principal=person,
                    acting_role=person.role,
                    action=PermissionGrant.Action.PUBLISH,
                    scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                    product=task.product,
                    platform_code=account.platform_code,
                    account_ref=account.account_code,
                )
                if decision.allowed:
                    people.append(person)
        who_zh, who_en = _turn_people_label(people, "尚未配置该账号的发布人", "No publisher configured for this account")
        return {
            "who_zh": who_zh,
            "who_en": who_en,
            "action_zh": "打开发布队列，完成发布检查并登记结果",
            "action_en": "Open the publishing queue, run checks, and record the result",
            "note_zh": "Owner、Admin 或 Operator 都可以发布，但必须各自拥有这个平台账号的有效发布权限。",
            "note_en": "Owner, Admin, or Operator may publish only with their own live permission for this exact account.",
        }
    if task.current_state == Task.State.BLOCKED:
        person = assignee or creator
        who = _principal_turn_label(person)
        return {
            "who_zh": who,
            "who_en": who,
            "action_zh": "补齐缺失资料后重新检查",
            "action_en": "Resolve the missing information and check again",
            "note_zh": "阻塞原因和旧检查结果会保留。",
            "note_en": "The blocker and previous check remain in history.",
        }
    return {
        "who_zh": "无需继续处理",
        "who_en": "No further action",
        "action_zh": "流程已结束",
        "action_en": "The workflow is finished",
        "note_zh": "历史记录仍会保留。",
        "note_en": "The history remains available.",
    }


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


def _requires_inline_primary(task: Task) -> bool:
    return TaskCompilationContext.objects.filter(task_id=task.pk).exists()


def _inline_content_versions(task: Task):
    """Return only the latest editable inline version for this task."""

    candidates = ContentAssetVersion.objects.filter(
        content_asset__task=task,
        representation_kind=ContentAssetVersion.RepresentationKind.INLINE_TEXT,
    )
    if not _requires_inline_primary(task):
        candidates = candidates.filter(content_asset__asset_key="publishable-content")
    latest_id = (
        candidates
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
        return "assign", AssignmentForm(operators=_eligible_operators(task, user), **common)
    if task.current_state == Task.State.ASSIGNED and task.current_assignee_principal_id == user.pk and can_edit:
        return "start", StartWorkForm(**common)
    if task.current_state == Task.State.HUMAN_REWORK and task.current_assignee_principal_id == user.pk and can_edit:
        return "resume-work", StartWorkForm(**common)
    if task.current_state == Task.State.IN_PROGRESS and task.current_assignee_principal_id == user.pk and can_edit:
        return "deliver", DeliveryDoDForm(
            criteria=task.contract_version.dod_criteria,
            content_versions=_inline_content_versions(task),
            require_inline_primary=_requires_inline_primary(task),
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
    reassign_form=None,
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
    latest_assignment = task.assignments.order_by("-assignment_number").first()
    management_kind = _task_management_kind(user, task)
    may_reassign = bool(
        latest_assignment
        and task.current_state in {Task.State.ASSIGNED, Task.State.IN_PROGRESS}
        and _can_manage_assignment(user, task)
    )
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
        "platform_label": (
            {
                "TIKTOK": "TikTok",
                "PINTEREST": "Pinterest",
                "QUORA": "Quora",
                "REDDIT": "Reddit",
                "SHOPIFY": "Shopify",
                "GOOGLE": "Google",
                "YOUTUBE": "YouTube",
            }.get(compilation_context.channel_plan.platform_code, compilation_context.channel_plan.platform_code)
            if compilation_context
            else ""
        ),
        "environment_label_zh": (
            "正式环境"
            if compilation_context
            and compilation_context.capability_state.account_environment_binding.runtime_environment.environment_type
            == "PRODUCTION"
            else "测试环境"
        ),
        "environment_label_en": (
            compilation_context.capability_state.account_environment_binding.runtime_environment.get_environment_type_display()
            if compilation_context
            else ""
        ),
        "capability_label_zh": (
            {
                "OPEN": "可以进行人工发布",
                "CLOSED": "暂时不能发布",
                "UNKNOWN": "发布状态尚未确认",
            }.get(compilation_context.capability_state.state, "发布状态尚未确认")
            if compilation_context
            else ""
        ),
        "current_turn": _current_task_turn(task, compilation_context),
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
        "management_kind": management_kind,
        "cancel_form": cancel_form if cancel_form is not None else (
            CancelTaskForm(
                state_version=task.state_version,
                task_state=task.current_state,
            )
            if management_kind
            in {"delete-draft", "abandon", "withdraw-abandon"}
            else None
        ),
        "reassign_form": reassign_form if reassign_form is not None else (
            AssignmentForm(
                operators=_eligible_operators(task, user, exclude_current=True),
                state_version=task.state_version,
                current_assignment=latest_assignment,
            )
            if may_reassign
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
    all_tasks = list(action_center.tasks)
    for task in all_tasks:
        _decorate_task(task)
    paginator = Paginator(all_tasks, 10)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)
    tasks = page_obj.object_list
    can_create_task = _editable_profiles(request.user).exists()
    return render(
        request,
        "dashboard/home.html",
        {
            "tasks": tasks,
            "page_obj": page_obj,
            "task_count": len(all_tasks),
            "blocked_task_count": sum(task.current_state == Task.State.BLOCKED for task in all_tasks),
            "can_create_task": can_create_task,
            "action_center": action_center,
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
            expected_current_assignment_id=form.cleaned_data.get("expected_current_assignment_id"),
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


def _reassign_task(task: Task, user: Principal, grant, form: AssignmentForm) -> None:
    assignee = form.cleaned_data["assignee"]
    if not _authorization(assignee, task, PermissionGrant.Action.EDIT).allowed:
        raise PermissionDenied("ASSIGNEE_NO_LONGER_AUTHORIZED")
    TaskAssignment.record(
        task=task,
        assignee_principal=assignee,
        command_id=_subcommand_id(form.cleaned_data["command_id"], "reassignment"),
        expected_task_version=form.cleaned_data["expected_state_version"],
        assigned_by_principal=user,
        acting_role=user.role,
        permission_grant=grant,
        recorded_by_principal=user,
        expected_current_assignment_id=form.cleaned_data["expected_current_assignment_id"],
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
    requires_inline_primary = _requires_inline_primary(task)
    if (
        requires_inline_primary
        and delivery_mode != DeliveryDoDForm.DeliveryMode.SYSTEM_CONTENT
    ):
        raise ValidationError(
            "Daily Operations 发布任务必须送审系统内完整正文；外部链接只能作为参考。"
        )
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
                approved_rework = supersedes_submission.withdrawal_events.filter(
                    event_type=TaskStateEvent.EventType.APPROVED_REWORK_REQUESTED,
                    task_id=task.pk,
                ).exists()
                if not (
                    triggering_review.decision == ReviewDecision.Decision.CHANGES_REQUESTED
                    or (
                        triggering_review.decision == ReviewDecision.Decision.APPROVED
                        and approved_rework
                    )
                ):
                    raise ValidationError(
                        "上一份交付必须被审核要求修改，或由 Owner/Admin 正式退回制作。"
                    )

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
                or (
                    requires_inline_primary
                    and len(version.inline_content.strip())
                    < DAILY_OPERATIONS_MIN_INLINE_CHARS
                )
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
    if action in {"assign", "reassign"}:
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
                operators=_eligible_operators(task, request.user),
                state_version=task.state_version,
            )
            if not form.is_valid():
                return render(request, "dashboard/task_detail.html", _detail_context(task, request.user, action_kind=action, action_form=form), status=400)
            _assign_task(task, request.user, grant, form)
            messages.success(request, "任务已明确分配；只有当前负责人可以开始执行。")
        elif action == "reassign":
            latest_assignment = task.assignments.order_by("-assignment_number").first()
            form = AssignmentForm(
                request.POST,
                operators=_eligible_operators(task, request.user, exclude_current=True),
                state_version=task.state_version,
                current_assignment=latest_assignment,
            )
            if not form.is_valid():
                return render(
                    request,
                    "dashboard/task_detail.html",
                    _detail_context(task, request.user, reassign_form=form),
                    status=400,
                )
            _reassign_task(task, request.user, grant, form)
            messages.success(request, "执行人已改派；旧分配记录仍保留，新负责人现在可以继续处理。")
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
                require_inline_primary=_requires_inline_primary(task),
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
            form = CancelTaskForm(
                request.POST,
                state_version=task.state_version,
                task_state=task.current_state,
            )
            if not form.is_valid():
                return render(
                    request,
                    "dashboard/task_detail.html",
                    _detail_context(task, request.user, cancel_form=form),
                    status=400,
                )
            if not _can_cancel_task(request.user, task):
                raise PermissionDenied("CURRENT_CANCEL_TASK_AUTHORIZATION_REQUIRED")
            state_before_cancel = task.current_state
            submission = (
                task.submissions.order_by("-submission_number").first()
                if task.current_state == Task.State.UNDER_REVIEW
                else None
            )
            Task.cancel_task(
                task_id=task.pk,
                command_id=form.cleaned_data["command_id"],
                expected_state_version=form.cleaned_data["expected_state_version"],
                actor_principal=request.user,
                acting_role=request.user.role,
                permission_grant=grant,
                recorded_by_principal=request.user,
                reason=form.cleaned_data["reason"],
                submission_id=submission.pk if submission else None,
            )
            if state_before_cancel == Task.State.DRAFT:
                message = "草稿已从任务列表移除；历史记录仍然保留。"
            elif state_before_cancel == Task.State.UNDER_REVIEW:
                message = "送审已撤回，任务已放弃；旧提交和全部历史仍然保留。"
            else:
                message = "任务已放弃并从待办列表隐藏；全部历史仍然保留。"
            messages.success(request, message)
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
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "redirect": reverse("dashboard:task-detail", args=[task.pk]), "reload": True})
    return redirect("dashboard:task-detail", task_id=task.pk)
