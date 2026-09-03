from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import BooleanField, Case, Count, Exists, OuterRef, Q, Value, When
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from contentops.models import ContentAssetVersion, ReviewDecision, TaskSubmission
from dailyops.models import DailyBatchDispositionEvent
from intelligence.models import CollectionRun
from products.models import Product
from releasegate.models import Publication
from workflow.models import Task, TaskAssignment, TaskCheckRun, TaskStateEvent


HISTORY_PAGE_SIZE = 20

HISTORY_ACTIVE_STATES = {
    Task.State.DRAFT,
    Task.State.BLOCKED,
    Task.State.READY,
    Task.State.ASSIGNED,
    Task.State.IN_PROGRESS,
    Task.State.SUBMITTED,
    Task.State.UNDER_REVIEW,
    Task.State.HUMAN_REWORK,
    Task.State.APPROVED,
}
HISTORY_CATEGORIES = {"active", "completed", "cancelled", "hidden"}


def _viewable_history_product_ids(user: Principal) -> list:
    """Return products the current account may still view right now.

    Historical participation is not itself a back door to product data.  This
    deliberately uses the central resolver so expiry, revocation and DENY
    precedence are identical to the rest of the application.
    """

    products = (
        Product.objects.filter(
            Q(tasks__isnull=False) | Q(daily_batch_disposition_events__isnull=False)
        )
        .distinct()
        .only("id")
    )
    return [
        product.pk
        for product in products
        if resolve_authorization(
            principal=user,
            acting_role=user.role,
            action=PermissionGrant.Action.VIEW,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=product,
        ).allowed
    ]


def _history_tasks(user: Principal, *, visible_product_ids=None):
    task_ref = OuterRef("pk")
    if visible_product_ids is None:
        visible_product_ids = _viewable_history_product_ids(user)
    return (
        Task.objects.filter(product_id__in=visible_product_ids)
        .annotate(
            participated_as_creator=Case(
                When(created_by_principal=user, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            ),
            participated_as_assignee=Exists(
                TaskAssignment.objects.filter(
                    task_id=task_ref,
                    assignee_principal=user,
                )
            ),
            participated_as_content_creator=Exists(
                ContentAssetVersion.objects.filter(
                    content_asset__task_id=task_ref,
                    created_by_principal=user,
                )
            ),
            participated_as_submitter=Exists(
                TaskSubmission.objects.filter(
                    task_id=task_ref,
                    submitted_by_principal=user,
                )
            ),
            participated_as_reviewer=Exists(
                ReviewDecision.objects.filter(
                    submission__task_id=task_ref,
                    reviewer_principal=user,
                )
            ),
            participated_as_publisher=Exists(
                Publication.objects.filter(
                    submission__task_id=task_ref,
                ).filter(
                    Q(requested_by_principal=user)
                    | Q(gate_records__publisher_principal=user)
                    | Q(events__actor_principal=user)
                )
            ),
            participated_as_checker=Exists(
                TaskCheckRun.objects.filter(
                    task_id=task_ref,
                    evaluator_principal=user,
                )
            ),
            participated_as_flow_actor=Exists(
                TaskStateEvent.objects.filter(
                    task_id=task_ref,
                    actor_principal=user,
                )
            ),
        )
        .filter(
            Q(participated_as_creator=True)
            | Q(participated_as_assignee=True)
            | Q(participated_as_content_creator=True)
            | Q(participated_as_submitter=True)
            | Q(participated_as_reviewer=True)
            | Q(participated_as_publisher=True)
            | Q(participated_as_checker=True)
            | Q(participated_as_flow_actor=True)
        )
        .select_related("product")
        .order_by("-updated_at", "-created_at", "-id")
    )


def _participation_labels(task: Task) -> list[dict[str, str]]:
    labels: list[dict[str, str]] = []
    if task.participated_as_creator:
        labels.append({"zh": "创建", "en": "Created"})
    if task.participated_as_assignee or task.participated_as_content_creator:
        labels.append({"zh": "执行", "en": "Worked on"})
    if task.participated_as_submitter:
        labels.append({"zh": "提交", "en": "Submitted"})
    if task.participated_as_reviewer:
        labels.append({"zh": "审核", "en": "Reviewed"})
    if task.participated_as_publisher:
        labels.append({"zh": "发布处理", "en": "Publishing"})
    if (
        task.participated_as_checker or task.participated_as_flow_actor
    ) and not labels:
        labels.append({"zh": "流程处理", "en": "Workflow action"})
    return labels


def _hidden_daily_runs(user: Principal, *, visible_product_ids=None):
    """Return hidden Daily runs in which this exact account participated.

    A product-wide VIEW grant is necessary but is not sufficient.  The user
    must also have either started one of the immutable collection attempts or
    recorded the disposition decision.  This keeps the personal history from
    becoming a product-wide task browser.
    """

    if visible_product_ids is None:
        visible_product_ids = _viewable_history_product_ids(user)
    participated_batch_keys = CollectionRun.objects.filter(
        executed_by_principal=user,
    ).values("batch_key")
    return (
        DailyBatchDispositionEvent.objects.filter(
            product_id__in=visible_product_ids,
        )
        .filter(
            Q(principal=user) | Q(batch_key__in=participated_batch_keys)
        )
        .select_related("product", "principal")
        .order_by("-created_at", "-id")
    )


def _decorate_hidden_run(event: DailyBatchDispositionEvent, *, run=None) -> None:
    # Older databases may contain a disposition without a remaining display
    # query.  The immutable event is still useful and must remain visible.
    event.batch_query = (
        str(run.query_spec.get("query", "")).strip()
        if run is not None
        else ""
    )
    event.batch_started_at = run.created_at if run is not None else event.created_at
    event.has_batch_runs = run is not None


def _category_queryset(tasks, category: str):
    if category == "completed":
        return tasks.filter(current_state=Task.State.DONE)
    if category == "cancelled":
        return tasks.filter(current_state=Task.State.CANCELLED)
    return tasks.filter(current_state__in=HISTORY_ACTIVE_STATES)


@login_required
def my_task_history(request: HttpRequest) -> HttpResponse:
    """Show a compact, read-only history for the signed-in account only."""

    category = str(request.GET.get("category", "active")).lower()
    if category not in HISTORY_CATEGORIES:
        category = "active"

    # Resolve live product visibility exactly once. Counts share one aggregate
    # query rather than repeating the participation annotations three times.
    visible_product_ids = _viewable_history_product_ids(request.user)
    all_tasks = _history_tasks(
        request.user,
        visible_product_ids=visible_product_ids,
    )
    hidden_runs = _hidden_daily_runs(
        request.user,
        visible_product_ids=visible_product_ids,
    )
    counts = all_tasks.aggregate(
        active=Count("pk", filter=Q(current_state__in=HISTORY_ACTIVE_STATES)),
        completed=Count("pk", filter=Q(current_state=Task.State.DONE)),
        cancelled=Count("pk", filter=Q(current_state=Task.State.CANCELLED)),
    )
    counts["hidden"] = hidden_runs.count()
    is_hidden_category = category == "hidden"
    paginator = Paginator(
        hidden_runs if is_hidden_category else _category_queryset(all_tasks, category),
        HISTORY_PAGE_SIZE,
    )
    page_obj = paginator.get_page(request.GET.get("page"))
    if is_hidden_category:
        events_by_batch = {event.batch_key: event for event in page_obj.object_list}
        representative_runs = {}
        for run in CollectionRun.objects.filter(
            batch_key__in=events_by_batch,
        ).order_by("batch_key", "created_at", "id"):
            event = events_by_batch.get(run.batch_key)
            if event is None or str(run.query_spec.get("product_id", "")) != str(event.product_id):
                continue
            representative_runs.setdefault(run.batch_key, run)
        for event in page_obj.object_list:
            _decorate_hidden_run(
                event,
                run=representative_runs.get(event.batch_key),
            )
    else:
        for task in page_obj.object_list:
            task.participation_labels = _participation_labels(task)
    return render(
        request,
        "dashboard/my_task_history.html",
        {
            "page_obj": page_obj,
            "category": category,
            "counts": counts,
            "is_hidden_category": is_hidden_category,
        },
    )
