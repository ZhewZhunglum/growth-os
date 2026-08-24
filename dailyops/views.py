from __future__ import annotations

import uuid
from collections import OrderedDict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
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
from dailyops.evidence_services import invalidate_evidence
from dailyops.deployment import build_web_daily_operations_runtime
from dailyops.services import (
    PLATFORMS,
    accept_daily_analysis,
    accept_analysis_and_start_execution_project,
    batch_runs,
    compile_channel_plan_task,
    confirm_channel_plan_and_compile_task,
    create_channel_plan,
    create_initiative_from_opportunity,
    ensure_default_sources,
    ingest_csv_text,
    ingest_manual_link,
    propose_daily_analysis,
    run_automatic_collection,
    run_platform_collection,
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


def _collectible_accounts(user: Principal, product: Product):
    account_ids = [
        account.pk
        for account in ChannelAccount.objects.filter(status=ChannelAccount.Status.ACTIVE)
        if _can_collect_account(user, product, account)
    ]
    return ChannelAccount.objects.filter(pk__in=account_ids).order_by(
        "platform_code", "display_name", "account_code"
    )


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
            rendered = []
            for key, values in error.message_dict.items():
                text = "；".join(render_value(value) for value in values)
                # Django uses __all__ for form-wide errors.  That is an
                # implementation detail, not a meaningful label for users.
                rendered.append(text if key == "__all__" else f"{key}：{text}")
            return "；".join(rendered)
        return "；".join(error.messages)
    return str(error)


def _copy(request: HttpRequest, chinese: str, english: str) -> str:
    return english if str(request.LANGUAGE_CODE).lower().startswith("en") else chinese


def _form_error_messages(form) -> list[str]:
    rendered: list[str] = []
    for field_name, errors in form.errors.as_data().items():
        label = "" if field_name == "__all__" else str(form.fields[field_name].label or field_name)
        for error in errors:
            rendered.extend(
                f"{label}：{message}" if label else str(message)
                for message in error.messages
            )
    return rendered


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
    form = DailyBatchForm(products=products, language_code=request.LANGUAGE_CODE)
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
        _copy(
            request,
            f"七个平台的 {len(sources)} 条来源路线已就绪；这里只登记路线，没有调用任何 API。",
            f"{len(sources)} source routes for seven platforms are ready. No API was called.",
        ),
    )
    return redirect("dailyops:home")


@login_required
@require_POST
def batch_start(request: HttpRequest) -> HttpResponse:
    products = _visible_products(request.user)
    form = DailyBatchForm(
        request.POST,
        products=products,
        language_code=request.LANGUAGE_CODE,
    )
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
    messages.success(
        request,
        _copy(
            request,
            "已建立今天的七个平台采集清单。" if result.created else "这次重复提交没有生成第二份清单。",
            "Today's seven-platform checklist is ready."
            if result.created
            else "This request was already handled; no duplicate checklist was created.",
        ),
    )
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
        ExternalEvidenceItem.objects.filter(
            collection_run_id__in=run_ids,
            invalidation_event__isnull=True,
        )
        .select_related("source")
        .order_by("-observed_at", "id")
    )
    for item in evidence:
        item.invalidation_command_id = uuid.uuid4()
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
        plan.owner_flow_command_id = uuid.uuid4()
        plan.owner_flow_task_id = uuid.uuid4()
    if not evidence:
        current_step = 2
    elif proposal is None or opportunity is None:
        current_step = 3
    elif opportunity.current_state in {
        ProductOpportunity.State.PROPOSED,
        ProductOpportunity.State.TRIAGED,
    }:
        current_step = 4
    elif initiative is None or initiative.current_state != Initiative.State.ACTIVE:
        current_step = 5
    elif not plans or any(plan.compiled_context is None for plan in plans):
        current_step = 6
    else:
        current_step = 7
    accounts = _collectible_accounts(request.user, product)
    plan_form = ChannelPlanForm(
        accounts=accounts,
        language_code=request.LANGUAGE_CODE,
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
        "manual_form": ManualEvidenceForm(
            initial={"command_id": uuid.uuid4()},
            language_code=request.LANGUAGE_CODE,
        ),
        "csv_form": CSVTextForm(
            initial={"command_id": uuid.uuid4()},
            language_code=request.LANGUAGE_CODE,
        ),
        "command_id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "current_step": current_step,
        "owner_start_command_id": uuid.uuid4(),
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
                _copy(
                    request,
                    f"自动/API/浏览器采集已保存 {result.created_count} 条真实来源；{unresolved} 个平台仍可用 CSV 或人工链接补齐。",
                    f"Collection saved {result.created_count} sourced items; {unresolved} platforms still need a link or CSV fallback.",
                ),
            )
        elif result.created:
            messages.warning(
                request,
                _copy(
                    request,
                    "已安全尝试七个平台，但当前没有可用的 API 凭据或已配对浏览器；没有联网猜接口，请继续用 CSV/人工链接。",
                    "All seven platforms were checked safely, but no configured API or paired browser is available. Paste a link or CSV instead.",
                ),
            )
        else:
            messages.info(
                request,
                _copy(
                    request,
                    "这次重复提交已返回原结果，没有再次调用连接器。",
                    "This request returned its original result and did not call connectors again.",
                ),
            )
    except (ValidationError, IntelligenceError, IngestionValidationError, ImproperlyConfigured) as error:
        messages.error(
            request,
            _copy(request, "自动/API/浏览器采集没有执行：", "Collection did not run: ")
            + _error_text(error),
        )
    return redirect(_batch_url(product, batch_key))


@login_required
@require_POST
def platform_collect(request: HttpRequest, product_id, batch_key, platform_code) -> JsonResponse:
    product = _product_for_user(request.user, product_id)
    _require_edit(request.user, product)
    try:
        form = _command_form(request.POST)
        platform = _platform(platform_code)
        result = run_platform_collection(
            batch_key=batch_key,
            product=product,
            command_id=form.cleaned_data["command_id"],
            platform=platform,
            principal=request.user,
            acting_role=request.user.role,
            runtime=build_web_daily_operations_runtime(settings),
        )
    except (ValidationError, IntelligenceError, IngestionValidationError, ImproperlyConfigured) as error:
        return JsonResponse(
            {"ok": False, "platform": platform_code, "message": _error_text(error)},
            status=400,
        )
    return JsonResponse(
        {
            "ok": True,
            "platform": platform.value,
            "platform_name": PLATFORM_NAMES[platform],
            "availability": result.run.availability_state,
            "availability_name": AVAILABILITY_NAMES.get(
                result.run.availability_state,
                result.run.availability_state,
            ),
            "created_count": result.created_count,
            "fallback": not bool(result.evidence),
        }
    )


@login_required
@require_POST
def evidence_manual(request: HttpRequest, product_id, batch_key, platform_code) -> HttpResponse:
    product = _product_for_user(request.user, product_id)
    _require_edit(request.user, product)
    expected_platform = _platform(platform_code)
    form = ManualEvidenceForm(
        request.POST,
        expected_platform=expected_platform,
        language_code=request.LANGUAGE_CODE,
    )
    if not form.is_valid():
        messages.error(
            request,
            _copy(request, "人工来源没有保存：", "The source was not saved: ") + form.errors.as_text(),
        )
        return redirect(_batch_url(product, batch_key))
    try:
        result = ingest_manual_link(
            batch_key=batch_key,
            product=product,
            platform=form.cleaned_data["platform"],
            operation_key=form.cleaned_data["command_id"],
            external_url=form.cleaned_data["external_url"],
            external_content_id=form.cleaned_data["external_content_id"],
            title=form.cleaned_data["title"],
            content_text=form.cleaned_data["content_text"],
            collected_at=form.cleaned_data["collected_at"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(
            request,
            _copy(
                request,
                f"已保存 {result.created_count} 条带来源的内容。",
                f"Saved {result.created_count} sourced item(s).",
            ),
        )
    except (ValidationError, IntelligenceError, IngestionValidationError) as error:
        messages.error(
            request,
            _copy(request, "人工来源没有保存：", "The source was not saved: ") + _error_text(error),
        )
    return redirect(_batch_url(product, batch_key))


@login_required
@require_POST
def evidence_manual_unified(request: HttpRequest, product_id, batch_key) -> HttpResponse:
    product = _product_for_user(request.user, product_id)
    _require_edit(request.user, product)
    form = ManualEvidenceForm(request.POST, language_code=request.LANGUAGE_CODE)
    if not form.is_valid():
        messages.error(
            request,
            _copy(request, "补录内容没有保存：", "The entry was not saved: ") + form.errors.as_text(),
        )
        return redirect(_batch_url(product, batch_key))
    try:
        result = ingest_manual_link(
            batch_key=batch_key,
            product=product,
            platform=form.cleaned_data["platform"],
            operation_key=form.cleaned_data["command_id"],
            external_url=form.cleaned_data["external_url"],
            external_content_id=form.cleaned_data["external_content_id"],
            title=form.cleaned_data["title"],
            content_text=form.cleaned_data["content_text"],
            collected_at=form.cleaned_data["collected_at"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(
            request,
            _copy(
                request,
                f"已识别为 {PLATFORM_NAMES[form.cleaned_data['platform']]}，并补录 {result.created_count} 条来源。",
                f"Detected {PLATFORM_NAMES[form.cleaned_data['platform']]} and saved {result.created_count} sourced item(s).",
            ),
        )
    except (ValidationError, IntelligenceError, IngestionValidationError) as error:
        messages.error(
            request,
            _copy(request, "补录内容没有保存：", "The entry was not saved: ") + _error_text(error),
        )
    return redirect(_batch_url(product, batch_key))


@login_required
@require_POST
def evidence_csv(request: HttpRequest, product_id, batch_key, platform_code) -> HttpResponse:
    product = _product_for_user(request.user, product_id)
    _require_edit(request.user, product)
    expected_platform = _platform(platform_code)
    form = CSVTextForm(
        request.POST,
        expected_platform=expected_platform,
        language_code=request.LANGUAGE_CODE,
    )
    if not form.is_valid():
        messages.error(
            request,
            _copy(request, "CSV 文本没有保存：", "CSV text was not saved: ") + form.errors.as_text(),
        )
        return redirect(_batch_url(product, batch_key))
    try:
        result = ingest_csv_text(
            batch_key=batch_key,
            product=product,
            platform=form.cleaned_data["platform"],
            operation_key=form.cleaned_data["command_id"],
            csv_text=form.cleaned_data["csv_text"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(
            request,
            _copy(
                request,
                f"CSV 文本已转换为 {result.created_count} 条带来源的内容。",
                f"CSV text was converted to {result.created_count} sourced item(s).",
            ),
        )
    except (ValidationError, IntelligenceError, IngestionValidationError) as error:
        messages.error(
            request,
            _copy(request, "CSV 文本没有保存：", "CSV text was not saved: ") + _error_text(error),
        )
    return redirect(_batch_url(product, batch_key))


@login_required
@require_POST
def evidence_csv_unified(request: HttpRequest, product_id, batch_key) -> HttpResponse:
    product = _product_for_user(request.user, product_id)
    _require_edit(request.user, product)
    form = CSVTextForm(request.POST, language_code=request.LANGUAGE_CODE)
    if not form.is_valid():
        messages.error(
            request,
            _copy(request, "CSV 文本没有保存：", "CSV text was not saved: ") + form.errors.as_text(),
        )
        return redirect(_batch_url(product, batch_key))
    try:
        result = ingest_csv_text(
            batch_key=batch_key,
            product=product,
            platform=form.cleaned_data["platform"],
            operation_key=form.cleaned_data["command_id"],
            csv_text=form.cleaned_data["csv_text"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(
            request,
            _copy(
                request,
                f"CSV 文本已补录 {result.created_count} 条来源。",
                f"CSV text added {result.created_count} sourced item(s).",
            ),
        )
    except (ValidationError, IntelligenceError, IngestionValidationError) as error:
        messages.error(
            request,
            _copy(request, "CSV 文本没有保存：", "CSV text was not saved: ") + _error_text(error),
        )
    return redirect(_batch_url(product, batch_key))


@login_required
@require_POST
def evidence_invalidate(request: HttpRequest, product_id, batch_key, evidence_id) -> HttpResponse:
    product = _product_for_user(request.user, product_id)
    _require_edit(request.user, product)
    try:
        command = _command_form(request.POST)
        result = invalidate_evidence(
            evidence_id=evidence_id,
            product=product,
            batch_key=batch_key,
            command_id=command.cleaned_data["command_id"],
            reason=request.POST.get("reason", "录入错误，不再用于后续分析。"),
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(
            request,
            _copy(
                request,
                "已从本次分析中移除；原始历史仍保留。" if result.created else "这条来源此前已经移除。",
                "Removed from this analysis; the original audit history remains."
                if result.created
                else "This source was already removed.",
            ),
        )
    except (ValidationError, IntelligenceError) as error:
        messages.error(
            request,
            _copy(request, "不能移除这条来源：", "This source cannot be removed: ") + _error_text(error),
        )
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
            _copy(
                request,
                "已生成选题建议。它还不是决定，需要你确认是否采用。",
                "The idea proposal is ready. It is not a decision until you accept it.",
            ),
        )
    except (ValidationError, IntelligenceError, IngestionValidationError, ImproperlyConfigured) as error:
        messages.error(
            request,
            _copy(request, "不能生成选题建议：", "The proposal could not be generated: ") + _error_text(error),
        )
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
        messages.success(
            request,
            _copy(
                request,
                f"已采用这个选题：{opportunity.title}。",
                f"This idea was accepted: {opportunity.title}.",
            ),
        )
    except (ValidationError, IntelligenceError) as error:
        messages.error(
            request,
            _copy(request, "不能采用这个选题：", "This idea could not be accepted: ") + _error_text(error),
        )
    return redirect(_batch_url(product, batch_key))


@login_required
@require_POST
def analysis_accept_and_start(request: HttpRequest, product_id, batch_key, proposal_id) -> HttpResponse:
    """Owner shortcut: one click, separate immutable planning facts."""

    product = _product_for_user(request.user, product_id)
    _require_edit(request.user, product)
    proposal = get_object_or_404(SignalAssessment, pk=proposal_id)
    try:
        form = _command_form(request.POST)
        result = accept_analysis_and_start_execution_project(
            proposal=proposal,
            product=product,
            command_id=form.cleaned_data["command_id"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(
            request,
            _copy(
                request,
                f"已采用建议并建立执行项目：{result.initiative.title}。",
                f"Suggestion accepted and execution project started: {result.initiative.title}.",
            ),
        )
    except (ValidationError, IntelligenceError) as error:
        messages.error(
            request,
            _copy(
                request,
                "不能采用建议并建立执行项目：",
                "The suggestion could not be turned into an execution project: ",
            )
            + _error_text(error),
        )
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
        messages.success(
            request,
            _copy(
                request,
                f"选题状态已更新为 {result.aggregate.current_state}。",
                f"Idea status is now {result.aggregate.current_state}.",
            ),
        )
    except (ValidationError, IntelligenceError) as error:
        messages.error(
            request,
            _copy(request, "状态没有改变：", "The status was not changed: ") + _error_text(error),
        )
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
        messages.success(
            request,
            _copy(
                request,
                f"已建立执行项目：{initiative.title}。",
                f"Execution project created: {initiative.title}.",
            ),
        )
    except (ValidationError, IntelligenceError) as error:
        messages.error(
            request,
            _copy(request, "不能建立执行项目：", "The execution project could not be created: ")
            + _error_text(error),
        )
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
        messages.success(
            request,
            _copy(
                request,
                f"执行项目状态已更新为 {result.aggregate.current_state}。",
                f"Execution project status is now {result.aggregate.current_state}.",
            ),
        )
    except (ValidationError, IntelligenceError) as error:
        messages.error(
            request,
            _copy(request, "状态没有改变：", "The status was not changed: ") + _error_text(error),
        )
    return redirect(_batch_url(product, _opportunity_batch_key(initiative.opportunity)))


@login_required
@require_POST
def plan_create(request: HttpRequest, initiative_id) -> HttpResponse:
    initiative = get_object_or_404(Initiative.objects.select_related("product", "opportunity"), pk=initiative_id)
    product = _product_for_user(request.user, initiative.product_id)
    _require_edit(request.user, product)
    accounts = _collectible_accounts(request.user, product)
    form = ChannelPlanForm(
        request.POST,
        accounts=accounts,
        language_code=request.LANGUAGE_CODE,
    )
    try:
        if not form.is_valid():
            raise ValidationError(_form_error_messages(form))
        plan = create_channel_plan(
            initiative=initiative,
            platform=Platform(form.cleaned_data["platform"]),
            command_id=form.cleaned_data["command_id"],
            plan_date=form.cleaned_data["plan_date"],
            goal={"title": form.cleaned_data["task_title"]},
            content_requirements={
                "task_title": form.cleaned_data["task_title"],
                "task_description": form.cleaned_data["task_description"],
            },
            principal=request.user,
            acting_role=request.user.role,
            channel_account=form.cleaned_data["channel_account"],
        )
        messages.success(
            request,
            _copy(
                request,
                f"已建立 {plan.platform_code} 平台任务安排。",
                f"{plan.platform_code} platform work plan created.",
            ),
        )
    except (ValidationError, IntelligenceError) as error:
        messages.error(
            request,
            _copy(request, "不能建立平台任务安排：", "The platform work plan could not be created: ")
            + _error_text(error),
        )
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
        messages.success(
            request,
            _copy(
                request,
                f"平台任务安排已更新为 {result.aggregate.current_state}。",
                f"Platform work plan status is now {result.aggregate.current_state}.",
            ),
        )
    except (ValidationError, IntelligenceError) as error:
        messages.error(
            request,
            _copy(request, "状态没有改变：", "The status was not changed: ") + _error_text(error),
        )
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
            _copy(
                request,
                "已生成执行任务。" if result.created else "这次重复提交没有生成第二个任务。",
                "Execution task created."
                if result.created
                else "This request was already handled; no duplicate task was created.",
            ),
        )
        return redirect("dashboard:task-detail", task_id=result.task.pk)
    except (ValidationError, IntelligenceError, PermissionDenied) as error:
        if isinstance(error, PermissionDenied):
            raise
        messages.error(
            request,
            _copy(request, "不能生成任务：", "The task could not be created: ") + _error_text(error),
        )
    return redirect(_batch_url(product, _opportunity_batch_key(plan.initiative.opportunity)))


@login_required
@require_POST
def plan_confirm_and_compile(request: HttpRequest, plan_id) -> HttpResponse:
    """Owner shortcut: confirm plan facts, activate, then compile one Task."""

    plan = get_object_or_404(
        ChannelPlan.objects.select_related("initiative__product", "initiative__opportunity"),
        pk=plan_id,
    )
    product = _product_for_user(request.user, plan.initiative.product_id)
    _require_edit(request.user, product)
    form = CompileTaskForm(request.POST)
    try:
        if not form.is_valid():
            raise ValidationError(form.errors.get_json_data())
        result = confirm_channel_plan_and_compile_task(
            channel_plan=plan,
            task_id=form.cleaned_data["task_id"],
            command_id=form.cleaned_data["command_id"],
            principal=request.user,
            acting_role=request.user.role,
        )
        messages.success(
            request,
            _copy(
                request,
                "平台安排已确认，执行任务已生成。"
                if result.compilation.created
                else "这次重复提交没有生成第二个任务。",
                "Platform plan confirmed and execution task created."
                if result.compilation.created
                else "This request was already handled; no duplicate task was created.",
            ),
        )
        return redirect("dashboard:task-detail", task_id=result.compilation.task.pk)
    except (ValidationError, IntelligenceError, PermissionDenied) as error:
        if isinstance(error, PermissionDenied):
            raise
        messages.error(
            request,
            _copy(
                request,
                "不能确认平台安排并生成任务：",
                "The platform plan could not be confirmed and compiled: ",
            )
            + _error_text(error),
        )
    return redirect(_batch_url(product, _opportunity_batch_key(plan.initiative.opportunity)))
