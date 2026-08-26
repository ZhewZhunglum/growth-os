from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q
from django.urls import reverse

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from contentops.models import TaskSubmission
from products.models import Product
from releasegate.models import ChannelAccount, Publication
from workflow.models import Task


@dataclass(frozen=True, slots=True)
class ActionCenterItem:
    key: str
    count: int
    label_zh: str
    label_en: str
    hint_zh: str
    hint_en: str
    url: str


@dataclass(frozen=True, slots=True)
class ActionCenter:
    tasks: tuple[Task, ...]
    items: tuple[ActionCenterItem, ...]
    waiting_items: tuple[ActionCenterItem, ...]
    total_count: int
    waiting_count: int
    pending_review_count: int
    pending_publish_count: int
    pending_complete_count: int
    can_open_review: bool
    can_open_publish: bool
    can_open_complete: bool
    primary: ActionCenterItem | None


def _product_allowed(user: Principal, task: Task, action: str) -> bool:
    return resolve_authorization(
        principal=user,
        acting_role=user.role,
        action=action,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=task.product,
    ).allowed


def _can_use_any_product(user: Principal, action: str) -> bool:
    """Use the same exact resolver as the endpoint; role names never unlock navigation."""

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


def _can_publish_any_account(user: Principal) -> bool:
    products = Product.objects.filter(product_status=Product.ProductStatus.ACTIVE).order_by("pk")
    accounts = ChannelAccount.objects.filter(status=ChannelAccount.Status.ACTIVE).order_by("pk")
    for product in products:
        for account in accounts:
            if resolve_authorization(
                principal=user,
                acting_role=user.role,
                action=PermissionGrant.Action.PUBLISH,
                scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                product=product,
                platform_code=account.platform_code,
                account_ref=account.account_code,
            ).allowed:
                return True
    return False


def _task_needs_user(task: Task, user: Principal) -> bool:
    """Return whether this task currently needs an action from this user.

    Being the creator or assignee is necessary but never sufficient: an
    expired/revoked grant removes the item from the workspace immediately.
    The role name is presentation metadata and is not used as authorization.
    """

    if task.current_state == Task.State.DRAFT:
        return bool(
            task.created_by_principal_id == user.pk
            and _product_allowed(user, task, PermissionGrant.Action.EDIT)
        )
    if task.current_state == Task.State.READY:
        return bool(
            task.created_by_principal_id == user.pk
            and _product_allowed(user, task, PermissionGrant.Action.ASSIGN_TASK)
        )
    if task.current_state == Task.State.BLOCKED:
        expected_principal_id = (
            task.current_assignee_principal_id or task.created_by_principal_id
        )
        return bool(
            expected_principal_id == user.pk
            and _product_allowed(user, task, PermissionGrant.Action.EDIT)
        )
    if task.current_state in {
        Task.State.ASSIGNED,
        Task.State.IN_PROGRESS,
        Task.State.HUMAN_REWORK,
    }:
        return bool(
            task.current_assignee_principal_id == user.pk
            and _product_allowed(user, task, PermissionGrant.Action.EDIT)
        )
    return False


def _actionable_tasks(user: Principal) -> tuple[Task, ...]:
    candidates = (
        Task.objects.filter(
            Q(current_assignee_principal=user) | Q(created_by_principal=user),
            current_state__in=[
                Task.State.DRAFT,
                Task.State.BLOCKED,
                Task.State.READY,
                Task.State.ASSIGNED,
                Task.State.IN_PROGRESS,
                Task.State.HUMAN_REWORK,
            ],
        )
        .select_related(
            "product",
            "contract_version",
            "current_assignee_principal",
            "created_by_principal",
        )
        .distinct()
        .order_by("updated_at", "title")
    )
    return tuple(task for task in candidates if _task_needs_user(task, user))


def build_action_center(user: Principal) -> ActionCenter:
    """Build a permission-filtered inbox from existing immutable workflow state."""

    if not getattr(user, "is_authenticated", False) or not user.can_authenticate:
        return ActionCenter(
            tasks=(),
            items=(),
            waiting_items=(),
            total_count=0,
            waiting_count=0,
            pending_review_count=0,
            pending_publish_count=0,
            pending_complete_count=0,
            can_open_review=False,
            can_open_publish=False,
            can_open_complete=False,
            primary=None,
        )

    # Imported lazily to avoid coupling app initialization to the review and
    # release slice. These helpers perform the same fail-closed checks as the
    # destination queues, including the narrow audited Owner self-approval
    # exception and the self-review prohibition for every other account.
    from dashboard.review_views import (
        _allowed_accounts,
        _can_complete,
        _can_review_submission,
    )

    tasks = _actionable_tasks(user)

    pending_review_count = 0
    for task in Task.objects.filter(current_state=Task.State.UNDER_REVIEW).select_related(
        "product"
    ):
        submission = (
            TaskSubmission.objects.filter(task=task)
            .select_related("submitted_by_principal")
            .order_by("-submission_number")
            .first()
        )
        if submission is not None and _can_review_submission(user, task, submission):
            pending_review_count += 1

    pending_publish_count = 0
    pending_complete_count = 0
    for task in Task.objects.filter(current_state=Task.State.APPROVED).select_related(
        "product"
    ):
        submission = task.submissions.order_by("-submission_number").first()
        has_manual_publication_proof = bool(
            submission
            and submission.publications.filter(
                status=Publication.Status.MANUAL_PUBLISHED_RECORDED
            ).exists()
        )
        if _allowed_accounts(user, task).exists() and not has_manual_publication_proof:
            pending_publish_count += 1
        if _can_complete(user, task):
            pending_complete_count += 1

    items: list[ActionCenterItem] = []
    if tasks:
        items.append(
            ActionCenterItem(
                key="tasks",
                count=len(tasks),
                label_zh="我的执行任务",
                label_en="My tasks",
                hint_zh="从最上面一项继续",
                hint_en="Continue with the first item",
                url=reverse("dashboard:task-detail", args=[tasks[0].pk]),
            )
        )
    if pending_review_count:
        items.append(
            ActionCenterItem(
                key="review",
                count=pending_review_count,
                label_zh="等我审核或批准",
                label_en="Needs my review or approval",
                hint_zh="Owner 可最终批准自己提交的内容；其他账号仍禁止自审",
                hint_en="Owners may finally approve their own submissions; other accounts cannot self-review",
                url=reverse("dashboard:review-queue"),
            )
        )
    if pending_publish_count:
        items.append(
            ActionCenterItem(
                key="publish",
                count=pending_publish_count,
                label_zh="等我发布",
                label_en="Needs publishing",
                hint_zh="发布前仍会重新检查权限和门禁",
                hint_en="Permission and gate are rechecked before publishing",
                url=reverse("dashboard:release-queue"),
            )
        )
    if pending_complete_count:
        items.append(
            ActionCenterItem(
                key="complete",
                count=pending_complete_count,
                label_zh="等我确认完成",
                label_en="Needs completion check",
                hint_zh="核对发布证明后完成任务",
                hint_en="Check publication proof and finish the task",
                url=reverse("dashboard:release-queue"),
            )
        )

    waiting_items: list[ActionCenterItem] = []
    waiting_review_candidates = (
        Task.objects.filter(current_state=Task.State.UNDER_REVIEW)
        .filter(Q(created_by_principal=user) | Q(submissions__submitted_by_principal=user))
        .select_related("product")
        .prefetch_related("submissions")
        .distinct()
        .order_by("updated_at", "title")
    )
    for task in waiting_review_candidates:
        submission = task.submissions.order_by("-submission_number").first()
        if submission is None or _can_review_submission(user, task, submission):
            continue
        if not (
            _product_allowed(user, task, PermissionGrant.Action.VIEW)
            or _product_allowed(user, task, PermissionGrant.Action.EDIT)
        ):
            continue
        reviewers = []
        for principal in Principal.objects.filter(
            principal_type=Principal.PrincipalType.HUMAN_USER,
            principal_status=Principal.PrincipalStatus.ACTIVE,
            is_active=True,
        ).order_by("role", "display_name", "username"):
            if not _can_review_submission(principal, task, submission):
                continue
            reviewers.append(principal.display_name or principal.username)
        if not reviewers:
            waiting_items.append(
                ActionCenterItem(
                    key=f"review-blocked:{task.pk}",
                    count=1,
                    label_zh=f"审核受阻：{task.title}",
                    label_en=f"Review blocked: {task.title}",
                    hint_zh="尚未配置可审核账号，请 Owner 处理",
                    hint_en="No eligible reviewer is configured; ask the Owner to handle it",
                    url=reverse("dashboard:task-detail", args=[task.pk]),
                )
            )
            continue
        reviewer_names = "、".join(reviewers)
        waiting_items.append(
            ActionCenterItem(
                key=f"waiting-review:{task.pk}",
                count=1,
                label_zh=f"等待审核：{task.title}",
                label_en=f"Waiting for review: {task.title}",
                hint_zh=f"已交给 {reviewer_names}；你现在不用操作",
                hint_en=f"Handed to {reviewer_names}; no action is needed from you now",
                url=reverse("dashboard:task-detail", args=[task.pk]),
            )
        )

    total_count = sum(item.count for item in items)
    waiting_count = sum(item.count for item in waiting_items)
    can_open_review = bool(
        pending_review_count
        or _can_use_any_product(user, PermissionGrant.Action.REVIEW)
    )
    can_open_publish = bool(pending_publish_count or _can_publish_any_account(user))
    can_open_complete = bool(
        pending_complete_count
        or _can_use_any_product(user, PermissionGrant.Action.COMPLETE_TASK)
    )
    return ActionCenter(
        tasks=tasks,
        items=tuple(items),
        waiting_items=tuple(waiting_items),
        total_count=total_count,
        waiting_count=waiting_count,
        pending_review_count=pending_review_count,
        pending_publish_count=pending_publish_count,
        pending_complete_count=pending_complete_count,
        can_open_review=can_open_review,
        can_open_publish=can_open_publish,
        can_open_complete=can_open_complete,
        primary=items[0] if items else None,
    )
