from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import BooleanField, Case, Exists, OuterRef, Q, Value, When
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from contentops.models import ContentAssetVersion, ReviewDecision, TaskSubmission
from products.models import Product
from releasegate.models import Publication
from workflow.models import Task, TaskAssignment, TaskCheckRun, TaskStateEvent


HISTORY_PAGE_SIZE = 20


def _viewable_history_product_ids(user: Principal) -> list:
    """Return products the current account may still view right now.

    Historical participation is not itself a back door to product data.  This
    deliberately uses the central resolver so expiry, revocation and DENY
    precedence are identical to the rest of the application.
    """

    products = Product.objects.filter(tasks__isnull=False).distinct().only("id")
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


def _history_tasks(user: Principal):
    task_ref = OuterRef("pk")
    return (
        Task.objects.filter(product_id__in=_viewable_history_product_ids(user))
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


@login_required
def my_task_history(request: HttpRequest) -> HttpResponse:
    """Show a compact, read-only history for the signed-in account only."""

    paginator = Paginator(_history_tasks(request.user), HISTORY_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    for task in page_obj.object_list:
        task.participation_labels = _participation_labels(task)
    return render(
        request,
        "dashboard/my_task_history.html",
        {"page_obj": page_obj},
    )
