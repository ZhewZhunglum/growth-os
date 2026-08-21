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


def _is_english(request) -> bool:
    return str(getattr(request, "LANGUAGE_CODE", "zh-hans")).lower().startswith("en")


def _text(request, chinese: str, english: str) -> str:
    return english if _is_english(request) else chinese


_SERVICE_ERRORS_EN = {
    "只能为启用中的平台账号记录数据。": "Performance can only be recorded for an active platform account.",
    "Panel code cannot be blank.": "Question set code cannot be blank.",
    "Question cannot be blank.": "The question cannot be blank.",
    "This GEO panel version already exists with different immutable input.": "This question set version already exists with different saved details.",
    "This question number already exists with different immutable input.": "This question number already exists with different saved text.",
    "同名指标已经属于另一个数据域，已拒绝混用。": "A metric with this code already belongs to another data area.",
    "该指标的单位与已封存定义不一致。": "The unit does not match the saved metric definition.",
    "反馈工作台只接受人工输入或粘贴 CSV。": "This page only accepts manual entry or pasted CSV data.",
    "一次必须提交 1 到 100 行。": "Submit between 1 and 100 rows at a time.",
    "只能为已经记录发布证明的内容追加表现。": "Performance can only be added after publication has been confirmed.",
    "发布记录缺少确切 Release Gate。": "The published item is missing its exact release check.",
    "发布记录与平台账号不一致。": "The published item does not match the platform account.",
    "同一个提交编号不能用于不同内容。": "The same submission ID cannot be reused for different content.",
    "证据编号无效。": "The selected supporting result is invalid.",
    "GEO 证据不属于所选产品。": "The AI search result does not belong to the selected product.",
    "平台表现证据没有与该产品的 Channel Plan 建立关系。": "The performance result is not linked to this product's channel plan.",
    "不支持的证据类型。": "This supporting result type is not supported.",
}

_SERVICE_ERRORS_ZH = {
    "Panel code cannot be blank.": "问题组代码不能为空。",
    "Question cannot be blank.": "问题不能为空。",
    "This GEO panel version already exists with different immutable input.": "这个问题组版本已经存在，但保存的内容不同。",
    "This question number already exists with different immutable input.": "这个问题序号已经存在，但问题内容不同。",
}


def _localized_error(request, message: str) -> str:
    if _is_english(request):
        return _SERVICE_ERRORS_EN.get(message, message)
    return _SERVICE_ERRORS_ZH.get(message, message)


def _add_service_error(request, form, error):
    if isinstance(error, PermissionDenied):
        form.add_error(
            None,
            _text(
                request,
                "你当前没有执行这项操作的有效权限。",
                "You do not currently have permission to do this.",
            ),
        )
        return
    if hasattr(error, "message_dict"):
        for field, errors in error.message_dict.items():
            target = field if field in form.fields else None
            for message in errors:
                form.add_error(target, _localized_error(request, message))
        return
    for message in getattr(error, "messages", [str(error)]):
        form.add_error(None, _localized_error(request, message))


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
    language_code = getattr(request, "LANGUAGE_CODE", None)
    panel_version_form = panel_version or GEOPanelVersionForm(
        actor=request.user,
        language_code=language_code,
    )
    panel_item_form = panel_item or GEOPanelItemForm(
        actor=request.user,
        language_code=language_code,
    )
    geo_form = geo or GEOResultForm(actor=request.user, language_code=language_code)
    geo_results = GEOProbeResult.objects.filter(
        probe_run__created_by_principal=request.user
    )
    return {
        "manual_form": manual or PerformanceManualForm(actor=request.user, language_code=language_code),
        "csv_form": csv_form or PerformanceCsvForm(actor=request.user, language_code=language_code),
        "panel_version_form": panel_version_form,
        "panel_item_form": panel_item_form,
        "can_configure_geo": panel_version_form.fields["product"].queryset.exists(),
        "recent_panels": panel_item_form.fields["panel"].queryset.prefetch_related("items")[:12],
        "geo_form": geo_form,
        "can_record_geo": geo_form.fields["panel_item"].queryset.exists(),
        "learning_form": learning or LearningProposalForm(
            actor=request.user,
            evidence_choices=choices,
            language_code=language_code,
        ),
        "recent_performance": ChannelPerformanceObservation.objects.filter(
            recorded_by_principal=request.user
        ).select_related("channel_account", "metric_definition").order_by("-created_at")[:12],
        "recent_publication_performance": PublicationPerformanceObservation.objects.filter(
            recorded_by_principal=request.user
        ).select_related(
            "publication__current_gate__channel_account",
            "metric_definition",
        ).order_by("-created_at")[:12],
        "recent_geo": geo_results.select_related(
            "panel_item__panel__product", "probe_run"
        ).prefetch_related("citations").order_by("-recorded_at")[:12],
        "recent_learning": LearningVersion.objects.filter(
            created_by_principal=request.user
        ).select_related("product").order_by("-created_at")[:12],
        "performance_count": (
            ChannelPerformanceObservation.objects.filter(recorded_by_principal=request.user).count()
            + PublicationPerformanceObservation.objects.filter(recorded_by_principal=request.user).count()
        ),
        "geo_count": GEOMetricObservation.objects.filter(recorded_by_principal=request.user).count(),
        "geo_result_count": geo_results.count(),
        "geo_mention_count": geo_results.filter(brand_mentioned=True).count(),
        "geo_citation_count": geo_results.filter(citations__isnull=False).distinct().count(),
        "learning_count": LearningVersion.objects.filter(created_by_principal=request.user).count(),
    }


@login_required
@require_http_methods(["GET"])
def feedback_home(request):
    return render(request, "feedbackui/home.html", _forms_for(request))


@login_required
@require_POST
def performance_manual(request):
    form = PerformanceManualForm(
        request.POST,
        actor=request.user,
        language_code=getattr(request, "LANGUAGE_CODE", None),
    )
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
            _add_service_error(request, form, error)
        else:
            messages.success(
                request,
                _text(
                    request,
                    f"已保存 {len(observations)} 条平台表现数据。",
                    f"Saved {len(observations)} performance record(s).",
                ),
            )
            return redirect("feedback:home")
    return render(request, "feedbackui/home.html", _forms_for(request, manual=form), status=400)


@login_required
@require_POST
def performance_csv(request):
    form = PerformanceCsvForm(
        request.POST,
        actor=request.user,
        language_code=getattr(request, "LANGUAGE_CODE", None),
    )
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
            _add_service_error(request, form, error)
        else:
            messages.success(
                request,
                _text(
                    request,
                    f"已从粘贴内容保存 {len(observations)} 条表现数据。",
                    f"Saved {len(observations)} performance record(s) from the pasted data.",
                ),
            )
            return redirect("feedback:home")
    return render(request, "feedbackui/home.html", _forms_for(request, csv_form=form), status=400)


@login_required
@require_POST
def geo_panel_version(request):
    form = GEOPanelVersionForm(
        request.POST,
        actor=request.user,
        language_code=getattr(request, "LANGUAGE_CODE", None),
    )
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
            _add_service_error(request, form, error)
        else:
            messages.success(
                request,
                (
                    _text(
                        request,
                        "AI 搜索问题组已创建。",
                        "The AI search question set was created.",
                    )
                    if created
                    else _text(
                        request,
                        "这个问题组已经存在，没有重复创建。",
                        "This question set already exists; no duplicate was created.",
                    )
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
    form = GEOPanelItemForm(
        request.POST,
        actor=request.user,
        language_code=getattr(request, "LANGUAGE_CODE", None),
    )
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
            _add_service_error(request, form, error)
        else:
            messages.success(
                request,
                (
                    _text(request, "问题已添加。", "The question was added.")
                    if created
                    else _text(
                        request,
                        "这个问题已经存在，没有重复创建。",
                        "This question already exists; no duplicate was created.",
                    )
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
    form = GEOResultForm(
        request.POST,
        actor=request.user,
        language_code=getattr(request, "LANGUAGE_CODE", None),
    )
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
            _add_service_error(request, form, error)
        else:
            messages.success(
                request,
                _text(
                    request,
                    "AI 搜索结果已保存。",
                    "The AI search result was saved.",
                ),
            )
            return redirect("feedback:home")
    return render(request, "feedbackui/home.html", _forms_for(request, geo=form), status=400)


@login_required
@require_POST
def learning_proposal(request):
    choices = evidence_choices(actor=request.user)
    form = LearningProposalForm(
        request.POST,
        actor=request.user,
        evidence_choices=choices,
        language_code=getattr(request, "LANGUAGE_CODE", None),
    )
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
            _add_service_error(request, form, error)
        else:
            messages.success(
                request,
                _text(
                    request,
                    "建议已保存，等待人工决定；系统不会自动改规则或创建任务。",
                    "The proposal was saved for a human decision; it will not change rules or create tasks automatically.",
                ),
            )
            return redirect("feedback:home")
    return render(request, "feedbackui/home.html", _forms_for(request, learning=form), status=400)
