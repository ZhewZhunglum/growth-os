from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import PermissionGrant
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
    create_binding_version,
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
)
from products.models import Product, ProductProfileVersion
from releasegate.models import AccountEnvironmentBinding, CapabilityState, ChannelAccount, RuntimeEnvironment


FORBIDDEN_INPUT_MARKERS = ("secret", "password", "api_key", "apikey", "access_key", "private_key", "upload")


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
    return render(
        request,
        "dashboard/configuration_home.html",
        {"products": products, "can_manage_runtime": can_manage_runtime},
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


def _runtime_context(*, bound_form=None, bound_action="") -> dict:
    forms = {
        "account": ChannelAccountForm(),
        "environment": RuntimeEnvironmentForm(initial={"object_storage_namespace": "DISABLED_LINK_ONLY"}),
        "binding": BindingForm(),
        "capability": CapabilityForm(),
    }
    if bound_form is not None and bound_action in forms:
        forms[bound_action] = bound_form
    return {
        "forms": forms,
        "accounts": ChannelAccount.objects.order_by("platform_code", "account_code"),
        "environments": RuntimeEnvironment.objects.order_by("environment_code"),
        "bindings": AccountEnvironmentBinding.objects.select_related(
            "channel_account", "runtime_environment"
        ).order_by("channel_account__account_code", "runtime_environment__environment_code", "-binding_version"),
        "capabilities": CapabilityState.objects.select_related(
            "account_environment_binding__channel_account", "account_environment_binding__runtime_environment"
        ).order_by("account_environment_binding_id", "capability_code", "-state_version"),
    }


@login_required
def runtime_configuration(request: HttpRequest) -> HttpResponse:
    require_runtime_configuration(request.user)
    return render(request, "dashboard/runtime_configuration.html", _runtime_context())


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
    form = form_class(request.POST)
    try:
        _reject_secret_or_file_input(request)
        if not form.is_valid():
            return render(
                request,
                "dashboard/runtime_configuration.html",
                _runtime_context(bound_form=form, bound_action=action),
                status=400,
            )
        data = dict(form.cleaned_data)
        if action == "account":
            create_channel_account(actor=request.user, **data)
            message = "渠道账号已登记（系统没有保存密码或密钥）。"
        elif action == "environment":
            create_runtime_environment(actor=request.user, **data)
            message = "运行环境已登记（只保存空间名称）。"
        elif action == "binding":
            create_binding_version(actor=request.user, **data)
            message = "新的账号环境绑定版本已追加，旧快照未修改。"
        else:
            create_capability_version(actor=request.user, **data)
            message = "新的能力状态版本已追加，旧快照未修改。"
    except (PermissionDenied, ValidationError) as error:
        form.add_error(None, error)
        return render(
            request,
            "dashboard/runtime_configuration.html",
            _runtime_context(bound_form=form, bound_action=action),
            status=400,
        )
    messages.success(request, message)
    return redirect("dashboard:runtime-configuration")
