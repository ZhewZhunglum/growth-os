from __future__ import annotations

import uuid
from collections import OrderedDict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.authorization import require_authorization, resolve_authorization
from accounts.models import PermissionGrant, Principal
from integrations.connectors.types import Platform
from integrations.errors import IngestionValidationError
from intelligence.exceptions import IntelligenceError
from intelligence.models import (
    AvailabilityState,
    ChannelPlan,
    CollectionRun,
    ExternalEvidenceItem,
    Initiative,
    ProductOpportunity,
    SignalAssessment,
    SourceRegistry,
)
from intelligence.services import (
    transition_channel_plan,
    transition_initiative,
    transition_opportunity,
)
from products.models import Product
from releasegate.models import ChannelAccount

from dailyops.forms import (
    CSVTextForm,
    ChannelPlanForm,
    CommandForm,
    CompileTaskForm,
    DailyBatchForm,
    ManualEvidenceForm,
    TransitionForm,
)
from dailyops.deployment import build_web_daily_operations_runtime
from dailyops.services import (
    PLATFORMS,
    accept_daily_analysis,
    batch_runs,
    compile_channel_plan_task,
    create_channel_plan,
    create_initiative_from_opportunity,
    ensure_default_sources,
    ingest_csv_text,
    ingest_manual_link,
    propose_daily_analysis,
    run_automatic_collection,
    start_daily_batch,
)


PLATFORM_NAMES = {
    Platform.PINTEREST: "Pinterest",
    Platform.QUORA: "Quora",
    Platform.TIKTOK: "TikTok",
    Platform.SHOPIFY: "Shopify / 独立站",
    Platform.GOOGLE_SEARCH: "Google 搜索",
    Platform.GOOGLE_SEARCH_CONSOLE: "Google Search Console",
    Platform.GOOGLE_ANALYTICS_4: "Google Analytics 4",
}
AVAILABILITY_NAMES = {
    AvailabilityState.PRESENT: "已有数据",
    AvailabilityState.MISSING: "还没有数据",
    AvailabilityState.BLOCKED: "暂时受阻",
    AvailabilityState.UNAVAILABLE: "当前不可用",
}


def _can_product(user: Principal, product: Product, action: str) -> bool:
    return resolve_authorization(
        principal=user,
        acting_role=user.role,
        action=action,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=product,
    ).allowed


def _visible_products(user: Principal):
    if not getattr(user, "can_authenticate", False):
        return Product.objects.none()
    ids = [
        product.pk
        for product in Product.objects.filter(product_status=Product.ProductStatus.ACTIVE).order_by("name")
        if _can_product(user, product, PermissionGrant.Action.VIEW)
        or _can_product(user, product, PermissionGrant.Action.EDIT)
    ]
    return Product.objects.filter(pk__in=ids).order_by("name")


def _product_for_user(user: Principal, product_id) -> Product:
    product = get_object_or_404(Product, pk=product_id, product_status=Product.ProductStatus.ACTIVE)
    if not (
        _can_product(user, product, PermissionGrant.Action.VIEW)
        or _can_product(user, product, PermissionGrant.Action.EDIT)
    ):
        raise Http404("Product not found.")
    return product


def _require_edit(user: Principal, product: Product):
    return require_authorization(
        principal=user,
        acting_role=user.role,
        action=PermissionGrant.Action.EDIT,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=product,
    )


def _can_collect_account(user: Principal, product: Product, account: ChannelAccount) -> bool:
    return resolve_authorization(
        principal=user,
        acting_role=user.role,
        action=PermissionGrant.Action.COLLECT_READ_ONLY,
        scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
        product=product,
        platform_code=account.platform_code,
        account_ref=account.account_code,
    ).allowed


def _platform(value: str) -> Platform:
    try:
        return Platform(value)
    except ValueError as error:
        raise Http404("Platform not found.") from error


def _error_text(error: Exception) -> str:
    if isinstance(error, ValidationError):
        if hasattr(error, "message_dict"):
            def render_value(value):
                if isinstance(value, dict):
                    return str(value.get("message", value))
                return str(value)
            return "；".join(
                f"{key}：{'；'.join(render_value(value) for value in values)}"
                for key, values in error.message_dict.items()
            )
        return "；".join(error.messages)
    return str(error)


def _batch_url(product, batch_key) -> str:
    return reverse("dailyops:batch-detail", kwargs={"product_id": product.pk, "batch_key": batch_key})


def _opportunity_batch_key(opportunity: ProductOpportunity) -> uuid.UUID:
    prefix = "daily-"
    if not opportunity.opportunity_key.startswith(prefix):
        raise ValidationError("这个 Opportunity 不是由 Daily Operations 生成的。")
    return uuid.UUID(hex=opportunity.opportunity_key.removeprefix(prefix))


def _command_form(data) -> CommandForm:
    form = CommandForm(data)
    if not form.is_valid():
        raise ValidationError(form.errors.get_json_data())
    return form


@login_required
def home(request: HttpRequest) -> HttpResponse:
    products = _visible_products(request.user)
    form = DailyBatchForm(products=products)
    visible_ids = {str(pk) for pk in products.values_list("pk", flat=True)}
    recent: OrderedDict[uuid.UUID, dict] = OrderedDict()
    for run in CollectionRun.objects.select_related("source").order_by("-created_at")[:300]:
        product_id = str(run.query_spec.get("product_id", ""))
        if product_id not in visible_ids or run.batch_key in recent:
            continue
        product = next((item for item in products if str(item.pk) == product_id), None)
        if product:
            recent[run.batch_key] = {
                "batch_key": run.batch_key,
                "product": product,
                "query": run.query_spec.get("query", ""),
                "created_at": run.created_at,
            }
        if len(recent) >= 20:
            break
    can_setup_sources = resolve_authorization(
        principal=request.user,
        acting_role=request.user.role,
        action=PermissionGrant.Action.MANAGE_ACCOUNT,
        scope_kind=PermissionGrant.ScopeKind.GLOBAL,
    ).allowed
    return render(
        request,
        "dailyops/home.html",
        {
            "form": form,
            "recent_batches": recent.values(),
            "configured_source_count": SourceRegistry.objects.filter(
                status=SourceRegistry.Status.ACTIVE,
                source_key__startswith="daily-",
            ).count(),
            "expected_source_count": len(PLATFORMS) * 4,
            "can_setup_sources": can_setup_sources,
        },
    )


@login_required
@require_POST
def source_setup(request: HttpRequest) -> HttpResponse:
    sources = ensure_default_sources(principal=request.user, acting_role=request.user.role)
    messages.success(
        request,
        f"七个平台的 {len(sources)} 条来源路线已就绪；这里只登记路线，没有调用任何 API。",
    )
    return redirect("dailyops:home")


@login_required
@require_POST
def batch_start(request: HttpRequest) -> HttpResponse:
    products = _visible_products(request.user)
    form = DailyBatchForm(request.POST, products=products)
    if not form.is_valid():
        return render(request, "dailyops/home.html", {"form": form, "recent_batches": ()}, status=400)
    product = form.cleaned_data["product"]
    _require_edit(request.user, product)
    try:
        result = start_daily_batch(
            batch_key=form.cleaned_data["command_id"],
            product=product,
            query=form.cleaned_data["query"],
            window_start=form.cleaned_data["window_start"],
            window_end=form.cleaned_data["window_end"],
            principal=request.user,
            acting_role=request.user.role,
        )
    except (ValidationError, IntelligenceError, IngestionValidationError) as error:
        form.add_error(None, _error_text(error))
        return render(request, "dailyops/home.html", {"form": form, "recent_batches": ()}, status=400)
    messages.success(request, "已建立今天的七个平台采集清单。" if result.created else "这次重复提交没有生成第二份清单。")
    return redirect(_batch_url(product, result.batch_key))


@login_required
def batch_detail(request: HttpRequest, product_id, batch_key) -> HttpResponse:
    product = _product_for_user(request.user, product_id)
    try:
        runs = batch_runs(batch_key=batch_key, product=product)
    except ValidationError as error:
        raise Http404("Daily batch not found.") from error
    run_ids = [run.pk for run in runs]
    evidence = tuple(
        ExternalEvidenceItem.objects.filter(collection_run_id__in=run_ids)
        .select_related("source")
        .order_by("-observed_at", "id")
    )
    cards = []
    for platform in PLATFORMS:
        platform_runs = [run for run in runs if run.source.platform_code == platform.value]
        latest = max(platform_runs, key=lambda item: (item.created_at, str(item.pk)), default=None)
        platform_evidence = [item for item in evidence if item.platform_code == platform.value]
        state = latest.availability_state if latest else AvailabilityState.MISSING
        cards.append(
            {
                "platform": platform,
                "name": PLATFORM_NAMES[platform],
                "state": state,
                "state_name": AVAILABILITY_NAMES.get(state, state),
                "latest": latest,
                "runs": sorted(platform_runs, key=lambda item: item.created_at, reverse=True),
                "evidence": platform_evidence,
                "next_step": (
                    "这个平台已有证据，可以继续生成分析提案。"
                    if platform_evidence
                    else "请粘贴一个真实链接/内容 ID，或者粘贴 CSV 文本。"
                ),
                "manual_form": ManualEvidenceForm(initial={"command_id": uuid.uuid4()}),
                "csv_form": CSVTextForm(initial={"command_id": uuid.uuid4()}),
            }
        )
    proposal = (
        SignalAssessment.objects.filter(
            assessment_key=f"daily-analysis-{batch_key.hex}",
            method="AI_PROPOSAL",
        )
        .order_by("-version_number")
        .first()
    )
    opportunity = ProductOpportunity.objects.filter(opportunity_key=f"daily-{batch_key.hex}").first()
    initiative = opportunity.initiatives.order_by("created_at").first() if opportunity else None
    plans = list(
        initiative.channel_plans.select_related("channel_account").order_by("plan_date", "platform_code")
        if initiative
        else []
    )
    for plan in plans:
        plan.compiled_context = plan.compilation_contexts.select_related("task").order_by("created_at").first()
    accounts = ChannelAccount.objects.filter(status=ChannelAccount.Status.ACTIVE).order_by(
        "platform_code", "display_name"
    )
    plan_form = ChannelPlanForm(
        accounts=accounts,
        initial={
            "command_id": uuid.uuid4(),
            "task_title": opportunity.title if opportunity else "",
            "task_description": opportunity.recommendation if opportunity else "",
        },
    )
    context = {
        "product": product,
        "batch_key": batch_key,
        "query": runs[0].query_spec.get("query", ""),
        "platform_cards": cards,
        "evidence": evidence,
        "proposal": proposal,
        "opportunity": opportunity,
        "initiative": initiative,
        "plans": plans,
        "plan_form": plan_form,
        "command_id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
    }
    return render(request, "dailyops/batch_detail.html", context)


@login_required
@require_POST
def automatic_collect(request: HttpRequest, product_id, batch_key) -> HttpResponse:
    product = _product_for_user(request.user, product_id)
    _require_edit(request.user, product)
    try:
        form = _command_form(request.POST)
        runtime = build_web_daily_operations_runtime(settings)
        result = run_automatic_collection(
            batch_key=batch_key,
            product=product,
            command_id=form.cleaned_data["command_id"],
            principal=request.user,
            acting_role=request.user.role,
            runtime=runtime,
        )
        unresolved = sum(
            run.result_summary.get("connector_status")
            not in {"SUCCEEDED", "PARTIAL"}
            for run in result.runs
        )
        if result.created_count:
            messages.success(
                request,
                f"自动/API/浏览器采集已保存 {result.created_count} 条真实来源；{unresolved} 个平台仍可用 CSV 或人工链接补齐。",
            )
        elif result.created:
            messages.warning(
                request,
                "已安全尝试七个平台，但当前没有可用的 API 凭据或已配对浏览器；没有联网猜接口，请继续用 CSV/人工链接。",
            )
        else:
            messages.info(request, "这次重复提交已返回原结果，没有再次调用连接器。")
    except (ValidationError, IntelligenceError, IngestionValidationError, ImproperlyConfigured) as error:
        messages.error(request, "自动/API/浏览器采集没有执行：" + _error_text(error))
    return redirect(_batch_url(product, batch_key))


@login_required
@require_POST
def evidence_manual(request: HttpRequest, product_id, batch_key, platform_code) -> HttpResponse:
    product = _product_for_user(request.user, product_id)
    _require_edit(request.user, product)
    form = ManualEvidenceForm(request.POST)
    if not form.is_valid():
        messages.error(request, "人工证据没有保存：" + form.errors.as_text())
        return redirect(_batch_url(product, batch_key))
    try:
        result = ingest_manual_link(
            batch_key=batch_key,
            product=product,
            platform=_platform(platform_code),
            operation_key=form.cleaned_data["command_id"],
            external_url=form.cleaned_data["external_url"],
            external_content_id=form.cleaned_data["external_content_id"],
            title=form.cleaned_data["title"],
            content_text=form.cleaned_data["content_text"],
            collected_at=form.cleaned_data["collected_at"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(request, f"已保存 {result.created_count} 条带来源的证据。")
    except (ValidationError, IntelligenceError, IngestionValidationError) as error:
        messages.error(request, "人工证据没有保存：" + _error_text(error))
    return redirect(_batch_url(product, batch_key))


@login_required
@require_POST
def evidence_csv(request: HttpRequest, product_id, batch_key, platform_code) -> HttpResponse:
    product = _product_for_user(request.user, product_id)
    _require_edit(request.user, product)
    form = CSVTextForm(request.POST)
    if not form.is_valid():
        messages.error(request, "CSV 文本没有保存：" + form.errors.as_text())
        return redirect(_batch_url(product, batch_key))
    try:
        result = ingest_csv_text(
            batch_key=batch_key,
            product=product,
            platform=_platform(platform_code),
            operation_key=form.cleaned_data["command_id"],
            csv_text=form.cleaned_data["csv_text"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(request, f"CSV 文本已转换为 {result.created_count} 条带来源的证据。")
    except (ValidationError, IntelligenceError, IngestionValidationError) as error:
        messages.error(request, "CSV 文本没有保存：" + _error_text(error))
    return redirect(_batch_url(product, batch_key))


@login_required
@require_POST
def analysis_propose(request: HttpRequest, product_id, batch_key) -> HttpResponse:
    product = _product_for_user(request.user, product_id)
    _require_edit(request.user, product)
    try:
        _command_form(request.POST)
        proposal = propose_daily_analysis(
            batch_key=batch_key,
            product=product,
            principal=request.user,
            acting_role=request.user.role,
            provider=build_web_daily_operations_runtime(settings).ai_provider,
        )
        messages.success(
            request,
            f"已生成分析提案 {proposal.pk}（{proposal.model_reference}）。它还不是决定，需要人工接受。",
        )
    except (ValidationError, IntelligenceError, IngestionValidationError, ImproperlyConfigured) as error:
        messages.error(request, "不能生成提案：" + _error_text(error))
    return redirect(_batch_url(product, batch_key))


@login_required
@require_POST
def analysis_accept(request: HttpRequest, product_id, batch_key, proposal_id) -> HttpResponse:
    product = _product_for_user(request.user, product_id)
    _require_edit(request.user, product)
    proposal = get_object_or_404(SignalAssessment, pk=proposal_id)
    try:
        _command_form(request.POST)
        opportunity = accept_daily_analysis(
            proposal=proposal,
            product=product,
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(request, f"人工已接受提案，并创建 Opportunity：{opportunity.title}。")
    except (ValidationError, IntelligenceError) as error:
        messages.error(request, "不能接受提案：" + _error_text(error))
    return redirect(_batch_url(product, batch_key))


@login_required
@require_POST
def opportunity_transition(request: HttpRequest, opportunity_id) -> HttpResponse:
    opportunity = get_object_or_404(ProductOpportunity.objects.select_related("product"), pk=opportunity_id)
    product = _product_for_user(request.user, opportunity.product_id)
    _require_edit(request.user, product)
    form = TransitionForm(request.POST)
    try:
        if not form.is_valid():
            raise ValidationError(form.errors.get_json_data())
        result = transition_opportunity(
            opportunity_id=opportunity.pk,
            to_state=form.cleaned_data["to_state"],
            expected_version=form.cleaned_data["expected_version"],
            command_id=form.cleaned_data["command_id"],
            reason=form.cleaned_data["reason"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(request, f"Opportunity 已进入 {result.aggregate.current_state}。")
    except (ValidationError, IntelligenceError) as error:
        messages.error(request, "状态没有改变：" + _error_text(error))
    return redirect(_batch_url(product, _opportunity_batch_key(opportunity)))


@login_required
@require_POST
def initiative_create(request: HttpRequest, opportunity_id) -> HttpResponse:
    opportunity = get_object_or_404(ProductOpportunity.objects.select_related("product"), pk=opportunity_id)
    product = _product_for_user(request.user, opportunity.product_id)
    _require_edit(request.user, product)
    try:
        form = _command_form(request.POST)
        initiative = create_initiative_from_opportunity(
            opportunity=opportunity,
            command_id=form.cleaned_data["command_id"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(request, f"已创建 Initiative：{initiative.title}。")
    except (ValidationError, IntelligenceError) as error:
        messages.error(request, "不能创建 Initiative：" + _error_text(error))
    return redirect(_batch_url(product, _opportunity_batch_key(opportunity)))


@login_required
@require_POST
def initiative_transition(request: HttpRequest, initiative_id) -> HttpResponse:
    initiative = get_object_or_404(Initiative.objects.select_related("product", "opportunity"), pk=initiative_id)
    product = _product_for_user(request.user, initiative.product_id)
    _require_edit(request.user, product)
    form = TransitionForm(request.POST)
    try:
        if not form.is_valid():
            raise ValidationError(form.errors.get_json_data())
        result = transition_initiative(
            initiative_id=initiative.pk,
            to_state=form.cleaned_data["to_state"],
            expected_version=form.cleaned_data["expected_version"],
            command_id=form.cleaned_data["command_id"],
            reason=form.cleaned_data["reason"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(request, f"Initiative 已进入 {result.aggregate.current_state}。")
    except (ValidationError, IntelligenceError) as error:
        messages.error(request, "状态没有改变：" + _error_text(error))
    return redirect(_batch_url(product, _opportunity_batch_key(initiative.opportunity)))


@login_required
@require_POST
def plan_create(request: HttpRequest, initiative_id) -> HttpResponse:
    initiative = get_object_or_404(Initiative.objects.select_related("product", "opportunity"), pk=initiative_id)
    product = _product_for_user(request.user, initiative.product_id)
    _require_edit(request.user, product)
    account_ids = [
        account.pk
        for account in ChannelAccount.objects.filter(status=ChannelAccount.Status.ACTIVE)
        if _can_collect_account(request.user, product, account)
    ]
    accounts = ChannelAccount.objects.filter(pk__in=account_ids)
    form = ChannelPlanForm(request.POST, accounts=accounts)
    try:
        if not form.is_valid():
            raise ValidationError(form.errors.get_json_data())
        plan = create_channel_plan(
            initiative=initiative,
            platform=Platform(form.cleaned_data["platform"]),
            command_id=form.cleaned_data["command_id"],
            plan_date=form.cleaned_data["plan_date"],
            goal={"title": form.cleaned_data["task_title"]},
            content_requirements={
                "task_title": form.cleaned_data["task_title"],
                "task_description": form.cleaned_data["task_description"],
                "environment_code": form.cleaned_data["environment_code"],
                "capability_code": form.cleaned_data["capability_code"],
            },
            principal=request.user,
            acting_role=request.user.role,
            channel_account=form.cleaned_data["channel_account"],
        )
        messages.success(request, f"已创建 {plan.platform_code} ChannelPlan。")
    except (ValidationError, IntelligenceError) as error:
        messages.error(request, "不能创建 ChannelPlan：" + _error_text(error))
    return redirect(_batch_url(product, _opportunity_batch_key(initiative.opportunity)))


@login_required
@require_POST
def plan_transition(request: HttpRequest, plan_id) -> HttpResponse:
    plan = get_object_or_404(
        ChannelPlan.objects.select_related("initiative__product", "initiative__opportunity"), pk=plan_id
    )
    product = _product_for_user(request.user, plan.initiative.product_id)
    _require_edit(request.user, product)
    form = TransitionForm(request.POST)
    try:
        if not form.is_valid():
            raise ValidationError(form.errors.get_json_data())
        result = transition_channel_plan(
            channel_plan_id=plan.pk,
            to_state=form.cleaned_data["to_state"],
            expected_version=form.cleaned_data["expected_version"],
            command_id=form.cleaned_data["command_id"],
            reason=form.cleaned_data["reason"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(request, f"ChannelPlan 已进入 {result.aggregate.current_state}。")
    except (ValidationError, IntelligenceError) as error:
        messages.error(request, "状态没有改变：" + _error_text(error))
    return redirect(_batch_url(product, _opportunity_batch_key(plan.initiative.opportunity)))


@login_required
@require_POST
def plan_compile(request: HttpRequest, plan_id) -> HttpResponse:
    plan = get_object_or_404(
        ChannelPlan.objects.select_related("initiative__product", "initiative__opportunity"), pk=plan_id
    )
    product = _product_for_user(request.user, plan.initiative.product_id)
    _require_edit(request.user, product)
    form = CompileTaskForm(request.POST)
    try:
        if not form.is_valid():
            raise ValidationError(form.errors.get_json_data())
        result = compile_channel_plan_task(
            channel_plan=plan,
            task_id=form.cleaned_data["task_id"],
            command_id=form.cleaned_data["command_id"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(
            request,
            "已生成真实执行任务。" if result.created else "这次重复提交没有生成第二个任务。",
        )
        return redirect("dashboard:task-detail", task_id=result.task.pk)
    except (ValidationError, IntelligenceError, PermissionDenied) as error:
        if isinstance(error, PermissionDenied):
            raise
        messages.error(request, "不能生成任务：" + _error_text(error))
    return redirect(_batch_url(product, _opportunity_batch_key(plan.initiative.opportunity)))
