from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from insights.models import (
    ChannelPerformanceObservation,
    GEOMetricObservation,
    GEOProbeResult,
    LearningVersion,
    PublicationPerformanceObservation,
)

from .forms import (
    GEOPanelItemForm,
    GEOPanelVersionForm,
    GEOResultForm,
    LearningProposalForm,
    PerformanceCsvForm,
    PerformanceManualForm,
)
from .services import (
    add_geo_panel_item,
    create_geo_panel_version,
    evidence_choices,
    propose_learning,
    record_geo_result,
    record_performance_rows,
)


def _add_service_error(form, error):
    if isinstance(error, PermissionDenied):
        form.add_error(None, "你当前没有执行这项操作的有效权限。")
        return
    if hasattr(error, "message_dict"):
        for field, errors in error.message_dict.items():
            target = field if field in form.fields else None
            for message in errors:
                form.add_error(target, message)
        return
    for message in getattr(error, "messages", [str(error)]):
        form.add_error(None, message)


def _forms_for(
    request,
    *,
    manual=None,
    csv_form=None,
    panel_version=None,
    panel_item=None,
    geo=None,
    learning=None,
):
    choices = evidence_choices(actor=request.user)
    panel_version_form = panel_version or GEOPanelVersionForm(actor=request.user)
    panel_item_form = panel_item or GEOPanelItemForm(actor=request.user)
    return {
        "manual_form": manual or PerformanceManualForm(actor=request.user),
        "csv_form": csv_form or PerformanceCsvForm(actor=request.user),
        "panel_version_form": panel_version_form,
        "panel_item_form": panel_item_form,
        "can_configure_geo": panel_version_form.fields["product"].queryset.exists(),
        "recent_panels": panel_item_form.fields["panel"].queryset.prefetch_related("items")[:12],
        "geo_form": geo or GEOResultForm(actor=request.user),
        "learning_form": learning or LearningProposalForm(actor=request.user, evidence_choices=choices),
        "recent_performance": ChannelPerformanceObservation.objects.filter(
            recorded_by_principal=request.user
        ).select_related("channel_account", "metric_definition").order_by("-created_at")[:12],
        "recent_publication_performance": PublicationPerformanceObservation.objects.filter(
            recorded_by_principal=request.user
        ).select_related(
            "publication__current_gate__channel_account",
            "metric_definition",
        ).order_by("-created_at")[:12],
        "recent_geo": GEOProbeResult.objects.filter(
            probe_run__created_by_principal=request.user
        ).select_related("panel_item__panel__product", "probe_run").order_by("-recorded_at")[:12],
        "recent_learning": LearningVersion.objects.filter(
            created_by_principal=request.user
        ).select_related("product").order_by("-created_at")[:12],
        "performance_count": (
            ChannelPerformanceObservation.objects.filter(recorded_by_principal=request.user).count()
            + PublicationPerformanceObservation.objects.filter(recorded_by_principal=request.user).count()
        ),
        "geo_count": GEOMetricObservation.objects.filter(recorded_by_principal=request.user).count(),
        "learning_count": LearningVersion.objects.filter(created_by_principal=request.user).count(),
    }


@login_required
@require_http_methods(["GET"])
def feedback_home(request):
    return render(request, "feedbackui/home.html", _forms_for(request))


@login_required
@require_POST
def performance_manual(request):
    form = PerformanceManualForm(request.POST, actor=request.user)
    if form.is_valid():
        try:
            observations = record_performance_rows(
                actor=request.user,
                channel_account=form.cleaned_data["channel_account"],
                publication=form.cleaned_data["publication"],
                rows=[form.observation_row()],
                source_kind="MANUAL",
                operation_key=form.cleaned_data["operation_key"],
            )
        except (ValidationError, PermissionDenied) as error:
            _add_service_error(form, error)
        else:
            messages.success(request, f"已追加 {len(observations)} 条平台表现记录；旧记录没有被修改。")
            return redirect("feedback:home")
    return render(request, "feedbackui/home.html", _forms_for(request, manual=form), status=400)


@login_required
@require_POST
def performance_csv(request):
    form = PerformanceCsvForm(request.POST, actor=request.user)
    if form.is_valid():
        try:
            observations = record_performance_rows(
                actor=request.user,
                channel_account=form.cleaned_data["channel_account"],
                publication=form.cleaned_data["publication"],
                rows=form.csv_rows,
                source_kind="CSV",
                operation_key=form.cleaned_data["operation_key"],
            )
        except (ValidationError, PermissionDenied) as error:
            _add_service_error(form, error)
        else:
            messages.success(request, f"已从粘贴内容追加 {len(observations)} 条表现数据，没有上传文件。")
            return redirect("feedback:home")
    return render(request, "feedbackui/home.html", _forms_for(request, csv_form=form), status=400)


@login_required
@require_POST
def geo_panel_version(request):
    form = GEOPanelVersionForm(request.POST, actor=request.user)
    if form.is_valid():
        try:
            panel, created = create_geo_panel_version(
                actor=request.user,
                product=form.cleaned_data["product"],
                panel_key=form.cleaned_data["panel_key"],
                version_number=form.cleaned_data["version_number"],
                market_code=form.cleaned_data["market_code"],
                language_code=form.cleaned_data["language_code"],
            )
        except (ValidationError, PermissionDenied) as error:
            _add_service_error(form, error)
        else:
            messages.success(
                request,
                (
                    f"已创建 GEO 问题组 {panel.panel_key} v{panel.version_number}。"
                    if created
                    else f"该 GEO 问题组版本已经存在，没有重复创建。"
                ),
            )
            return redirect("feedback:home")
    return render(
        request,
        "feedbackui/home.html",
        _forms_for(request, panel_version=form),
        status=400,
    )


@login_required
@require_POST
def geo_panel_item(request):
    form = GEOPanelItemForm(request.POST, actor=request.user)
    if form.is_valid():
        try:
            item, created = add_geo_panel_item(
                actor=request.user,
                panel=form.cleaned_data["panel"],
                item_number=form.cleaned_data["item_number"],
                question=form.cleaned_data["question"],
                intent=form.cleaned_data["intent"],
            )
        except (ValidationError, PermissionDenied) as error:
            _add_service_error(form, error)
        else:
            messages.success(
                request,
                (
                    f"已为 {item.panel.panel_key} v{item.panel.version_number} 添加第 {item.item_number} 个问题。"
                    if created
                    else "该问题已经存在，没有重复创建。"
                ),
            )
            return redirect("feedback:home")
    return render(
        request,
        "feedbackui/home.html",
        _forms_for(request, panel_item=form),
        status=400,
    )


@login_required
@require_POST
def geo_result(request):
    form = GEOResultForm(request.POST, actor=request.user)
    if form.is_valid():
        try:
            result = record_geo_result(
                actor=request.user,
                panel_item=form.cleaned_data["panel_item"],
                provider=form.cleaned_data["provider"],
                model_reference=form.cleaned_data["model_reference"],
                availability_state=form.cleaned_data["availability_state"],
                response_text=form.cleaned_data["response_text"],
                brand_mentioned=form.cleaned_data["brand_mentioned"],
                rank_position=form.cleaned_data["rank_position"],
                citation_urls=form.cleaned_data["citation_urls"],
                operation_key=form.cleaned_data["operation_key"],
            )
        except (ValidationError, PermissionDenied) as error:
            _add_service_error(form, error)
        else:
            messages.success(request, f"已追加 GEO 结果 {result.pk}，并明确区分真实 0 与缺失。")
            return redirect("feedback:home")
    return render(request, "feedbackui/home.html", _forms_for(request, geo=form), status=400)


@login_required
@require_POST
def learning_proposal(request):
    choices = evidence_choices(actor=request.user)
    form = LearningProposalForm(request.POST, actor=request.user, evidence_choices=choices)
    if form.is_valid():
        try:
            learning = propose_learning(
                actor=request.user,
                product=form.cleaned_data["product"],
                learning_key=form.cleaned_data["learning_key"],
                title=form.cleaned_data["title"],
                conclusion=form.cleaned_data["conclusion"],
                recommended_action=form.cleaned_data["recommended_action"],
                confidence=form.cleaned_data["confidence"],
                evidence_ref=form.cleaned_data["evidence_ref"],
                evidence_note=form.cleaned_data["evidence_note"],
            )
        except (ValidationError, PermissionDenied) as error:
            _add_service_error(form, error)
        else:
            messages.success(
                request,
                f"学习提案 {learning.learning_key} v{learning.version_number} 已保存为“待人工决定”；不会自动改规则或创建任务。",
            )
            return redirect("feedback:home")
    return render(request, "feedbackui/home.html", _forms_for(request, learning=form), status=400)
