from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import PermissionGrant
from dailyops.forms import platform_label
from dashboard.config_forms import (
    BindingForm,
    CapabilityForm,
    ChannelAccountForm,
    ClaimMatrixForm,
    EvidenceLibraryForm,
    ObjectiveProfileForm,
    ProductProfileForm,
    ProfileAssetLinkForm,
    RuntimeEnvironmentForm,
    SealProfileForm,
)
from dashboard.config_services import (
    add_profile_evidence_link,
    create_capability_version,
    create_channel_account,
    create_claim_matrix,
    create_evidence_library,
    create_objective_profile,
    create_product_profile,
    create_runtime_environment,
    require_product_configuration,
    require_runtime_configuration,
    seal_and_activate_profile,
    set_current_account_environment,
)
from dashboard.runtime_summary import (
    build_environment_summaries,
    build_platform_summaries,
    current_account_environments,
    platform_anchor,
)
from integrations.connectors.types import Platform
from products.models import Product, ProductProfileVersion
from releasegate.models import AccountEnvironmentBinding, CapabilityState, ChannelAccount, RuntimeEnvironment
from releasegate.runtime import inspect_manual_publish_context


FORBIDDEN_INPUT_MARKERS = ("secret", "password", "api_key", "apikey", "access_key", "private_key", "upload")


def _ui_text(language_code: str, zh: str, en: str) -> str:
    return en if str(language_code or "").lower().startswith("en") else zh


def _reject_secret_or_file_input(request: HttpRequest) -> None:
    if request.FILES:
        raise ValidationError("V1 不接收文件上传。")
    suspicious = [
        key for key in request.POST
        if key != "csrfmiddlewaretoken"
        and (
            key.lower() in {"file", "attachment", "media"}
            or key.lower().endswith("_file")
            or any(marker in key.lower() for marker in FORBIDDEN_INPUT_MARKERS)
        )
    ]
    if suspicious:
        raise ValidationError("这里不能填写密码、密钥或文件；只保存外部链接和引用名称。")


def _manageable_products(actor):
    rows = []
    for product in Product.objects.select_related("current_profile_version").order_by("name", "product_code"):
        try:
            require_product_configuration(actor, product)
        except PermissionDenied:
            continue
        rows.append(product)
    return rows


@login_required
def configuration_home(request: HttpRequest) -> HttpResponse:
    products = _manageable_products(request.user)
    can_manage_runtime = True
    try:
        require_runtime_configuration(request.user)
    except PermissionDenied:
        can_manage_runtime = False
    runtime_overview = None
    if can_manage_runtime:
        platform_summaries = build_platform_summaries()
        runtime_overview = {
            "ready_execution_count": sum(
                summary.ready for summary in platform_summaries if summary.kind == "EXECUTION"
            ),
            "execution_count": sum(summary.kind == "EXECUTION" for summary in platform_summaries),
            "registered_analysis_count": sum(
                summary.state == "REGISTERED"
                for summary in platform_summaries
                if summary.kind == "ANALYTICAL"
            ),
            "environment_count": RuntimeEnvironment.objects.filter(
                status=RuntimeEnvironment.Status.ACTIVE
            ).count(),
        }
    return render(
        request,
        "dashboard/configuration_home.html",
        {
            "products": products,
            "can_manage_runtime": can_manage_runtime,
            "runtime_overview": runtime_overview,
        },
    )


def _product_context(product: Product, *, bound_form=None, bound_action="") -> dict:
    forms = {
        "objective": ObjectiveProfileForm(initial={"objective_key": f"{product.product_code}_DAILY"}),
        "claim": ClaimMatrixForm(),
        "evidence": EvidenceLibraryForm(),
        "profile": ProductProfileForm(product=product),
        "asset": ProfileAssetLinkForm(product=product),
        "seal": SealProfileForm(product=product),
    }
    if bound_form is not None and bound_action in forms:
        forms[bound_action] = bound_form
    return {
        "product": product,
        "forms": forms,
        "profiles": product.profile_versions.select_related(
            "objective_profile_version", "claim_matrix_version", "evidence_library_version"
        ).prefetch_related("asset_links").order_by("-version_number"),
        "claim_matrices": product.claim_matrices.prefetch_related("items__product_claim_version").order_by("-version_number"),
        "evidence_libraries": product.evidence_libraries.prefetch_related(
            "items__controlled_evidence_item_version"
        ).order_by("-version_number"),
    }


@login_required
def product_configuration(request: HttpRequest, product_id) -> HttpResponse:
    product = get_object_or_404(Product, pk=product_id)
    require_product_configuration(request.user, product)
    return render(request, "dashboard/product_configuration.html", _product_context(product))


@login_required
@require_POST
def product_configuration_action(request: HttpRequest, product_id, action: str) -> HttpResponse:
    product = get_object_or_404(Product, pk=product_id)
    require_product_configuration(request.user, product)
    forms = {
        "objective": ObjectiveProfileForm,
        "claim": ClaimMatrixForm,
        "evidence": EvidenceLibraryForm,
        "profile": ProductProfileForm,
        "asset": ProfileAssetLinkForm,
        "seal": SealProfileForm,
    }
    form_class = forms.get(action)
    if form_class is None:
        raise PermissionDenied("未知配置动作。")
    kwargs = {"product": product} if action in {"profile", "asset", "seal"} else {}
    form = form_class(request.POST, **kwargs)
    try:
        _reject_secret_or_file_input(request)
        if not form.is_valid():
            return render(
                request,
                "dashboard/product_configuration.html",
                _product_context(product, bound_form=form, bound_action=action),
                status=400,
            )
        data = dict(form.cleaned_data)
        if action == "objective":
            create_objective_profile(actor=request.user, product=product, **data)
            message = "新的目标版本已创建并封存。"
        elif action == "claim":
            data["market_code"] = product.market_code
            create_claim_matrix(actor=request.user, product=product, claim=data)
            message = "新的声明及声明矩阵已创建并封存。"
        elif action == "evidence":
            create_evidence_library(actor=request.user, product=product, evidence=data)
            message = "新的外部证据链接及证据库已创建并封存。"
        elif action == "profile":
            create_product_profile(actor=request.user, product=product, **data)
            message = "新的产品档案草稿已创建；确认链接后再封存启用。"
        elif action == "asset":
            add_profile_evidence_link(
                actor=request.user,
                profile=data["profile"],
                evidence=data["evidence"],
                asset_kind=data["asset_kind"],
            )
            message = "外部链接已加入档案草稿。"
        else:
            seal_and_activate_profile(actor=request.user, profile=data["profile"])
            message = "产品档案已封存并设为当前版本，旧版本继续保留。"
    except (PermissionDenied, ValidationError) as error:
        form.add_error(None, error)
        return render(
            request,
            "dashboard/product_configuration.html",
            _product_context(product, bound_form=form, bound_action=action),
            status=400,
        )
    messages.success(request, message)
    return redirect("dashboard:product-configuration", product_id=product.pk)


def _active_account(account_id) -> ChannelAccount | None:
    if not account_id:
        return None
    try:
        return ChannelAccount.objects.filter(
            pk=account_id,
            status=ChannelAccount.Status.ACTIVE,
        ).first()
    except (ValidationError, ValueError):
        return None


def _binding_history_rows(bindings, *, at) -> tuple[dict, ...]:
    seen_pairs = set()
    rows = []
    for binding in bindings:
        pair = (binding.channel_account_id, binding.runtime_environment_id)
        is_latest = pair not in seen_pairs
        seen_pairs.add(pair)
        is_current = bool(
            is_latest
            and binding.status == AccountEnvironmentBinding.Status.ACTIVE
            and binding.valid_from <= at
            and (binding.valid_until is None or binding.valid_until > at)
            and binding.channel_account.status == ChannelAccount.Status.ACTIVE
            and binding.runtime_environment.status == RuntimeEnvironment.Status.ACTIVE
        )
        if is_current:
            label_zh, label_en, state = "当前连接", "Current connection", "current"
        elif is_latest:
            label_zh, label_en, state = "最新记录（非当前连接）", "Latest record (not current)", "latest"
        else:
            label_zh, label_en, state = "历史版本", "Historical version", "history"
        rows.append(
            {
                "binding": binding,
                "label_zh": label_zh,
                "label_en": label_en,
                "state": state,
                "is_current": is_current,
            }
        )
    return tuple(rows)


def _capability_history_rows(capabilities, *, current_binding_ids) -> tuple[dict, ...]:
    seen_pairs = set()
    rows = []
    for capability in capabilities:
        pair = (capability.account_environment_binding_id, capability.capability_code)
        is_latest = pair not in seen_pairs
        seen_pairs.add(pair)
        if is_latest and capability.account_environment_binding_id in current_binding_ids:
            label_zh, label_en, state = "当前连接的最新记录", "Latest record for current connection", "current"
        elif is_latest:
            label_zh, label_en, state = "历史连接的最新记录", "Latest record for historical connection", "latest"
        else:
            label_zh, label_en, state = "历史版本", "Historical version", "history"
        rows.append(
            {
                "capability": capability,
                "label_zh": label_zh,
                "label_en": label_en,
                "state": state,
            }
        )
    return tuple(rows)


def _runtime_advanced_context(
    *,
    language_code="",
    bound_form=None,
    bound_action="",
    initial_account_id="",
    initial_binding_id="",
    open_action="",
) -> dict:
    focus_account = _active_account(initial_account_id)
    binding_initial = {"channel_account": focus_account.pk} if focus_account else None
    capability_initial = {"binding": initial_binding_id} if initial_binding_id else None
    forms = {
        "account": ChannelAccountForm(language_code=language_code),
        "environment": RuntimeEnvironmentForm(
            initial={"object_storage_namespace": "DISABLED_LINK_ONLY"},
            language_code=language_code,
        ),
        "binding": BindingForm(initial=binding_initial, language_code=language_code),
        "capability": CapabilityForm(initial=capability_initial, language_code=language_code),
    }
    if bound_form is not None and bound_action in forms:
        forms[bound_action] = bound_form
    bindings = list(
        AccountEnvironmentBinding.objects.select_related(
            "channel_account", "runtime_environment"
        ).order_by(
            "channel_account__account_code",
            "runtime_environment__environment_code",
            "-binding_version",
            "-created_at",
            "-id",
        )
    )
    capabilities = list(
        CapabilityState.objects.select_related(
            "account_environment_binding__channel_account",
            "account_environment_binding__runtime_environment",
        ).order_by(
            "account_environment_binding_id",
            "capability_code",
            "-state_version",
            "-created_at",
            "-id",
        )
    )
    at = timezone.now()
    binding_history = _binding_history_rows(bindings, at=at)
    current_binding_ids = {
        row["binding"].pk for row in binding_history if row["is_current"]
    }
    return {
        "forms": forms,
        "open_action": open_action or bound_action,
        "focus_account": focus_account,
        "focus_platform_zh": platform_label(focus_account.platform_code) if focus_account else "",
        "focus_platform_en": platform_label(focus_account.platform_code, english=True) if focus_account else "",
        "focus_contexts": current_account_environments(focus_account, at=at) if focus_account else (),
        "accounts": ChannelAccount.objects.order_by("platform_code", "account_code"),
        "environments": RuntimeEnvironment.objects.order_by("environment_code"),
        "binding_history": binding_history,
        "capability_history": _capability_history_rows(
            capabilities,
            current_binding_ids=current_binding_ids,
        ),
    }


def _runtime_summary_context(*, focus_platform="") -> dict:
    summaries = build_platform_summaries()
    execution = tuple(summary for summary in summaries if summary.kind == "EXECUTION")
    analytical = tuple(summary for summary in summaries if summary.kind == "ANALYTICAL")
    environments = build_environment_summaries()
    return {
        "execution_platforms": execution,
        "analytical_platforms": analytical,
        "environment_summaries": environments,
        "ready_execution_count": sum(summary.ready for summary in execution),
        "execution_count": len(execution),
        "registered_analysis_count": sum(summary.state == "REGISTERED" for summary in analytical),
        "focus_platform": focus_platform,
    }


@login_required
def runtime_configuration(request: HttpRequest) -> HttpResponse:
    require_runtime_configuration(request.user)
    focus_platform = str(request.GET.get("platform") or "").upper()
    return render(
        request,
        "dashboard/runtime_configuration.html",
        _runtime_summary_context(focus_platform=focus_platform),
    )


@login_required
def runtime_configuration_advanced(request: HttpRequest) -> HttpResponse:
    require_runtime_configuration(request.user)
    account_id = request.GET.get("account", "")
    initial_binding_id = request.GET.get("binding", "")
    requested_step = request.GET.get("step", "")
    open_action = "capability" if requested_step == "capability" else ("binding" if account_id else "")
    return render(
        request,
        "dashboard/runtime_configuration_advanced.html",
        _runtime_advanced_context(
            language_code=request.LANGUAGE_CODE,
            initial_account_id=account_id,
            initial_binding_id=initial_binding_id,
            open_action=open_action,
        ),
    )


@login_required
@require_POST
def runtime_configuration_action(request: HttpRequest, action: str) -> HttpResponse:
    require_runtime_configuration(request.user)
    forms = {
        "account": ChannelAccountForm,
        "environment": RuntimeEnvironmentForm,
        "binding": BindingForm,
        "capability": CapabilityForm,
    }
    form_class = forms.get(action)
    if form_class is None:
        raise PermissionDenied("未知运行配置动作。")
    form = form_class(request.POST, language_code=request.LANGUAGE_CODE)
    try:
        _reject_secret_or_file_input(request)
        if not form.is_valid():
            return render(
                request,
                "dashboard/runtime_configuration_advanced.html",
                _runtime_advanced_context(
                    language_code=request.LANGUAGE_CODE,
                    bound_form=form,
                    bound_action=action,
                    initial_account_id=request.POST.get("channel_account", "") if action == "binding" else "",
                    initial_binding_id=request.POST.get("binding", "") if action == "capability" else "",
                ),
                status=400,
            )
        data = dict(form.cleaned_data)
        if action == "account":
            create_channel_account(actor=request.user, **data)
            message = _ui_text(
                request.LANGUAGE_CODE,
                "渠道账号已登记（系统没有保存密码或密钥）。",
                "Channel account registered. No password or key was stored.",
            )
        elif action == "environment":
            create_runtime_environment(actor=request.user, **data)
            message = _ui_text(
                request.LANGUAGE_CODE,
                "使用场景已登记（只保存空间名称）。",
                "Usage context registered. Only namespace names were stored.",
            )
        elif action == "binding":
            data.pop("confirm_replace", None)
            change = set_current_account_environment(actor=request.user, **data)
            post_change = inspect_manual_publish_context(change.binding.channel_account)
            if change.created_target_version:
                message = _ui_text(
                    request.LANGUAGE_CODE,
                    "当前使用场景已切换，其他连接已追加为撤销记录。请继续第 4 步重新确认人工发布状态。",
                    "Current usage context switched and other connections received Revoked records. "
                    "Continue to Step 4 to confirm manual-publishing availability again.",
                )
            elif change.revoked_count and post_change.ready:
                message = _ui_text(
                    request.LANGUAGE_CODE,
                    "已保留所选使用场景，并安全结束其他当前连接；原有可用状态继续有效。",
                    "The selected usage context was kept and other current connections were safely ended; "
                    "its existing availability remains valid.",
                )
            elif change.revoked_count:
                message = _ui_text(
                    request.LANGUAGE_CODE,
                    "已保留所选使用场景并安全结束其他连接；请继续第 4 步确认人工发布是否可用。",
                    "The selected usage context was kept and other connections were safely ended. "
                    "Continue to Step 4 to confirm manual-publishing availability.",
                )
            elif post_change.ready:
                message = _ui_text(
                    request.LANGUAGE_CODE,
                    "所选使用场景已经是当前连接，没有创建重复记录。",
                    "The selected usage context is already current; no duplicate record was created.",
                )
            else:
                message = _ui_text(
                    request.LANGUAGE_CODE,
                    "所选使用场景已经是当前连接；请继续第 4 步确认人工发布是否可用。",
                    "The selected usage context is already current. Continue to Step 4 to confirm "
                    "manual-publishing availability.",
                )
        else:
            create_capability_version(actor=request.user, **data)
            message = _ui_text(
                request.LANGUAGE_CODE,
                "新的可用状态版本已追加，旧快照未修改。",
                "A new availability version was appended; the earlier snapshot was not changed.",
            )
    except (PermissionDenied, ValidationError) as error:
        form.add_error(None, error)
        return render(
            request,
            "dashboard/runtime_configuration_advanced.html",
            _runtime_advanced_context(
                language_code=request.LANGUAGE_CODE,
                bound_form=form,
                bound_action=action,
                initial_account_id=request.POST.get("channel_account", "") if action == "binding" else "",
                initial_binding_id=request.POST.get("binding", "") if action == "capability" else "",
            ),
            status=400,
        )
    messages.success(request, message)
    if action == "binding":
        if change.created_target_version or not post_change.ready:
            query = urlencode(
                {
                    "account": str(change.binding.channel_account_id),
                    "binding": str(change.binding.pk),
                    "step": "capability",
                }
            )
            return redirect(
                f'{reverse("dashboard:runtime-configuration-advanced")}?{query}#advanced-capability'
            )
        platform = Platform(change.binding.channel_account.platform_code)
        query = urlencode({"platform": platform.value})
        return redirect(
            f'{reverse("dashboard:runtime-configuration")}?{query}#{platform_anchor(platform)}'
        )
    return redirect("dashboard:runtime-configuration-advanced")
