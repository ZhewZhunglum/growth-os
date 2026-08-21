from __future__ import annotations

import csv
import io
import uuid
from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from insights.models import AvailabilityState, GEOProbePanel, GEOProbePanelItem
from products.models import Product
from releasegate.models import ChannelAccount, Publication


OBSERVATION_STATES = (
    (AvailabilityState.PRESENT, "有数据（0 也算真实数据）"),
    (AvailabilityState.MISSING, "本次没有拿到数据"),
    (AvailabilityState.BLOCKED, "被权限或平台阻止"),
    (AvailabilityState.UNAVAILABLE, "平台暂不提供"),
)


def _validate_operation_key(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValidationError("本次提交编号无效，请刷新页面后重试。") from error


class OperationForm(forms.Form):
    operation_key = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.initial.get("operation_key"):
            self.initial["operation_key"] = str(uuid.uuid4())

    def clean_operation_key(self):
        return _validate_operation_key(self.cleaned_data["operation_key"])


class PerformanceManualForm(OperationForm):
    channel_account = forms.ModelChoiceField(
        label="平台账号",
        queryset=ChannelAccount.objects.none(),
        empty_label="请选择账号",
    )
    publication = forms.ModelChoiceField(
        label="关联的发布记录",
        queryset=Publication.objects.none(),
        required=False,
        empty_label="不关联（仅记录账号整体表现）",
        help_text="Daily Task 发布后的数据请选择对应发布记录；账号汇总数据可以留空。",
    )
    metric_key = forms.SlugField(label="指标代码", max_length=120, help_text="例如 views、clicks")
    metric_name = forms.CharField(label="指标名称", max_length=240, help_text="例如 浏览量")
    availability_state = forms.ChoiceField(label="数据状态", choices=OBSERVATION_STATES)
    numeric_value = forms.DecimalField(
        label="数值",
        required=False,
        max_digits=28,
        decimal_places=8,
        help_text="选择“有数据”时必填；真实的 0 请直接填 0。",
    )
    unit = forms.CharField(label="单位", max_length=32, required=False, initial="count")
    observed_at = forms.DateTimeField(
        label="数据时间",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        help_text="不填则使用当前时间。",
    )
    source_reference = forms.CharField(
        label="来源说明或链接",
        max_length=1024,
        required=False,
        help_text="可填平台报表链接、页面 ID 或人工记录说明。",
    )

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        candidates = ChannelAccount.objects.filter(status=ChannelAccount.Status.ACTIVE).order_by(
            "platform_code", "account_code"
        )
        allowed_ids = []
        if actor and actor.is_authenticated:
            for account in candidates:
                decision = resolve_authorization(
                    principal=actor,
                    acting_role=actor.role,
                    action=PermissionGrant.Action.COLLECT_READ_ONLY,
                    scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                    account_ref=account.account_code,
                )
                if decision.allowed:
                    allowed_ids.append(account.pk)
        self.fields["channel_account"].queryset = candidates.filter(pk__in=allowed_ids)
        self.fields["publication"].queryset = Publication.objects.filter(
            status=Publication.Status.MANUAL_PUBLISHED_RECORDED,
            current_gate__channel_account_id__in=allowed_ids,
        ).select_related(
            "current_gate__channel_account",
            "submission__task",
        ).order_by("-created_at")

    def clean(self):
        cleaned = super().clean()
        state = cleaned.get("availability_state")
        value = cleaned.get("numeric_value")
        account = cleaned.get("channel_account")
        publication = cleaned.get("publication")
        if publication and account and publication.current_gate.channel_account_id != account.pk:
            self.add_error("publication", "这条发布记录不属于所选平台账号。")
        if state == AvailabilityState.PRESENT and value is None:
            self.add_error("numeric_value", "有数据时必须填写数值；0 是有效数值。")
        if state and state != AvailabilityState.PRESENT and value is not None:
            self.add_error("numeric_value", "没有拿到数据时请留空，不要用 0 代替缺失。")
        cleaned["observed_at"] = cleaned.get("observed_at") or timezone.now()
        return cleaned

    def observation_row(self) -> dict:
        return {
            "metric_key": self.cleaned_data["metric_key"],
            "metric_name": self.cleaned_data["metric_name"],
            "availability_state": self.cleaned_data["availability_state"],
            "numeric_value": self.cleaned_data["numeric_value"],
            "unit": self.cleaned_data["unit"],
            "observed_at": self.cleaned_data["observed_at"],
            "source_reference": self.cleaned_data["source_reference"],
        }


class PerformanceCsvForm(OperationForm):
    REQUIRED_HEADERS = {"metric_key", "metric_name", "availability_state", "numeric_value"}
    MAX_BYTES = 64 * 1024
    MAX_ROWS = 100

    channel_account = forms.ModelChoiceField(
        label="平台账号",
        queryset=ChannelAccount.objects.none(),
        empty_label="请选择账号",
    )
    publication = forms.ModelChoiceField(
        label="关联的发布记录",
        queryset=Publication.objects.none(),
        required=False,
        empty_label="不关联（仅记录账号整体表现）",
        help_text="CSV 全部行会绑定到同一条发布记录；账号汇总数据可以留空。",
    )
    csv_text = forms.CharField(
        label="粘贴 CSV 内容",
        widget=forms.Textarea(attrs={"rows": 10}),
        help_text=(
            "必需列：metric_key, metric_name, availability_state, numeric_value。"
            "可选列：unit, observed_at, source_reference。最多 100 行，不上传文件。"
        ),
    )

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        candidates = ChannelAccount.objects.filter(status=ChannelAccount.Status.ACTIVE).order_by(
            "platform_code", "account_code"
        )
        allowed_ids = []
        if actor and actor.is_authenticated:
            for account in candidates:
                decision = resolve_authorization(
                    principal=actor,
                    acting_role=actor.role,
                    action=PermissionGrant.Action.COLLECT_READ_ONLY,
                    scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                    account_ref=account.account_code,
                )
                if decision.allowed:
                    allowed_ids.append(account.pk)
        self.fields["channel_account"].queryset = candidates.filter(pk__in=allowed_ids)
        self.fields["publication"].queryset = Publication.objects.filter(
            status=Publication.Status.MANUAL_PUBLISHED_RECORDED,
            current_gate__channel_account_id__in=allowed_ids,
        ).select_related("current_gate__channel_account").order_by("-created_at")

    def clean(self):
        cleaned = super().clean()
        account = cleaned.get("channel_account")
        publication = cleaned.get("publication")
        if publication and account and publication.current_gate.channel_account_id != account.pk:
            self.add_error("publication", "这条发布记录不属于所选平台账号。")
        return cleaned

    def clean_csv_text(self):
        text = self.cleaned_data["csv_text"]
        if len(text.encode("utf-8")) > self.MAX_BYTES:
            raise ValidationError("CSV 内容过大；一次最多粘贴 64KB。")
        try:
            reader = csv.DictReader(io.StringIO(text, newline=""))
            headers = set(reader.fieldnames or [])
            if not self.REQUIRED_HEADERS.issubset(headers):
                missing = ", ".join(sorted(self.REQUIRED_HEADERS - headers))
                raise ValidationError(f"缺少 CSV 列：{missing}")
            unknown = headers - self.REQUIRED_HEADERS - {"unit", "observed_at", "source_reference"}
            if unknown:
                raise ValidationError(f"存在不支持的 CSV 列：{', '.join(sorted(unknown))}")
            raw_rows = list(reader)
        except csv.Error as error:
            raise ValidationError("CSV 格式无法解析，请检查引号和逗号。") from error
        if not raw_rows:
            raise ValidationError("CSV 至少需要一行数据。")
        if len(raw_rows) > self.MAX_ROWS:
            raise ValidationError("一次最多导入 100 行。")

        normalized = []
        valid_states = {choice[0] for choice in OBSERVATION_STATES}
        for index, row in enumerate(raw_rows, start=2):
            metric_key = (row.get("metric_key") or "").strip()
            metric_name = (row.get("metric_name") or "").strip()
            state = (row.get("availability_state") or "").strip().upper()
            raw_value = (row.get("numeric_value") or "").strip()
            for label, value in (("metric_key", metric_key), ("metric_name", metric_name)):
                if value.startswith(("=", "+", "@")):
                    raise ValidationError(f"第 {index} 行 {label} 不能以公式字符开头。")
            if not metric_key or not metric_name:
                raise ValidationError(f"第 {index} 行必须填写 metric_key 和 metric_name。")
            if state not in valid_states:
                raise ValidationError(f"第 {index} 行 availability_state 无效。")
            value = None
            if raw_value:
                try:
                    value = Decimal(raw_value)
                except InvalidOperation as error:
                    raise ValidationError(f"第 {index} 行 numeric_value 不是有效数字。") from error
            if state == AvailabilityState.PRESENT and value is None:
                raise ValidationError(f"第 {index} 行为 PRESENT，必须填写数值；0 可直接填 0。")
            if state != AvailabilityState.PRESENT and value is not None:
                raise ValidationError(f"第 {index} 行不是 PRESENT，numeric_value 必须留空。")
            observed_at = timezone.now()
            raw_observed_at = (row.get("observed_at") or "").strip()
            if raw_observed_at:
                field = forms.DateTimeField()
                try:
                    observed_at = field.clean(raw_observed_at)
                except ValidationError as error:
                    raise ValidationError(f"第 {index} 行 observed_at 不是有效时间。") from error
            normalized.append(
                {
                    "metric_key": metric_key,
                    "metric_name": metric_name,
                    "availability_state": state,
                    "numeric_value": value,
                    "unit": (row.get("unit") or "").strip(),
                    "observed_at": observed_at,
                    "source_reference": (row.get("source_reference") or "").strip(),
                }
            )
        self.csv_rows = normalized
        return text


def _can_manage_geo_panels(actor) -> bool:
    return bool(
        actor
        and actor.is_authenticated
        and actor.role in {
            Principal.Role.OWNER,
            Principal.Role.OPERATIONS_ADMIN,
        }
    )


class GEOPanelVersionForm(forms.Form):
    panel_key = forms.SlugField(
        label="GEO 问题组代码",
        max_length=120,
        help_text="例如 puko-focus-us；同一代码用版本号区分历史。",
    )
    version_number = forms.IntegerField(
        label="版本号",
        min_value=1,
        help_text="明确填写 1、2、3……；重复提交同一版本不会重复创建。",
    )
    product = forms.ModelChoiceField(
        label="产品",
        queryset=Product.objects.none(),
        empty_label="请选择产品",
    )
    market_code = forms.CharField(label="市场", max_length=16, initial="US")
    language_code = forms.CharField(label="问题语言", max_length=16, initial="en")

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        products = Product.objects.filter(
            product_status=Product.ProductStatus.ACTIVE
        ).order_by("product_code")
        allowed_ids = []
        if _can_manage_geo_panels(actor):
            for product in products:
                if resolve_authorization(
                    principal=actor,
                    acting_role=actor.role,
                    action=PermissionGrant.Action.EDIT,
                    scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                    product=product,
                ).allowed:
                    allowed_ids.append(product.pk)
        self.fields["product"].queryset = products.filter(pk__in=allowed_ids)

    def clean_market_code(self):
        return self.cleaned_data["market_code"].strip().upper()

    def clean_language_code(self):
        return self.cleaned_data["language_code"].strip().lower()


class GEOPanelItemForm(forms.Form):
    panel = forms.ModelChoiceField(
        label="确切问题组版本",
        queryset=GEOProbePanel.objects.none(),
        empty_label="请选择问题组版本",
    )
    item_number = forms.IntegerField(
        label="问题序号",
        min_value=1,
        help_text="同一问题组内唯一；重复提交同一内容不会重复创建。",
    )
    question = forms.CharField(
        label="要问 AI 的问题",
        max_length=5_000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    intent = forms.CharField(
        label="这个问题想了解什么",
        max_length=120,
        required=False,
        help_text="例如 product discovery、brand comparison。",
    )

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        panels = GEOProbePanel.objects.select_related("product").order_by(
            "panel_key", "-version_number"
        )
        allowed_ids = []
        if _can_manage_geo_panels(actor):
            for panel in panels:
                if resolve_authorization(
                    principal=actor,
                    acting_role=actor.role,
                    action=PermissionGrant.Action.EDIT,
                    scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                    product=panel.product,
                ).allowed:
                    allowed_ids.append(panel.pk)
        self.fields["panel"].queryset = panels.filter(pk__in=allowed_ids)

    def clean_question(self):
        return self.cleaned_data["question"].strip()

    def clean_intent(self):
        return self.cleaned_data["intent"].strip()


class GEOResultForm(OperationForm):
    panel_item = forms.ModelChoiceField(
        label="GEO 问题",
        queryset=GEOProbePanelItem.objects.none(),
        empty_label="请选择问题",
    )
    provider = forms.CharField(label="回答平台", max_length=64, help_text="例如 DeepSeek、ChatGPT、Perplexity")
    model_reference = forms.CharField(label="模型或页面版本", max_length=160, help_text="例如 deepseek-v4-flash")
    availability_state = forms.ChoiceField(label="结果状态", choices=OBSERVATION_STATES)
    response_text = forms.CharField(label="回答原文", required=False, widget=forms.Textarea(attrs={"rows": 7}))
    brand_mentioned = forms.BooleanField(label="回答是否提到 PUKO", required=False)
    rank_position = forms.IntegerField(label="出现顺序", required=False, min_value=1)
    citation_urls = forms.CharField(
        label="引用链接",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="每行一个 URL；系统只保存链接，不下载网页或文件。",
    )

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        candidates = GEOProbePanelItem.objects.select_related(
            "panel", "panel__product"
        ).order_by("panel__panel_key", "panel__version_number", "item_number")
        allowed_ids = []
        if actor and actor.is_authenticated:
            for item in candidates:
                decision = resolve_authorization(
                    principal=actor,
                    acting_role=actor.role,
                    action=PermissionGrant.Action.COLLECT_READ_ONLY,
                    scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                    product=item.panel.product,
                )
                if decision.allowed:
                    allowed_ids.append(item.pk)
        self.fields["panel_item"].queryset = candidates.filter(pk__in=allowed_ids)

    def clean(self):
        cleaned = super().clean()
        state = cleaned.get("availability_state")
        response = (cleaned.get("response_text") or "").strip()
        citations = [line.strip() for line in (cleaned.get("citation_urls") or "").splitlines() if line.strip()]
        url_field = forms.URLField()
        valid_urls = []
        for index, url in enumerate(citations, start=1):
            try:
                valid_urls.append(url_field.clean(url))
            except ValidationError as error:
                self.add_error("citation_urls", f"第 {index} 个引用不是有效 URL。")
                break
        if state == AvailabilityState.PRESENT and not response:
            self.add_error("response_text", "有结果时必须粘贴回答原文。")
        if state and state != AvailabilityState.PRESENT:
            if response:
                self.add_error("response_text", "没有拿到结果时不要填写回答原文。")
            if cleaned.get("brand_mentioned") or cleaned.get("rank_position") or valid_urls:
                self.add_error(None, "没有拿到结果时不能填写品牌、排名或引用。")
        cleaned["response_text"] = response
        cleaned["citation_urls"] = valid_urls
        return cleaned


class LearningProposalForm(OperationForm):
    product = forms.ModelChoiceField(label="产品", queryset=Product.objects.none(), empty_label="请选择产品")
    learning_key = forms.SlugField(label="学习主题代码", max_length=120, help_text="例如 short-hook-test")
    title = forms.CharField(label="标题", max_length=240)
    conclusion = forms.CharField(label="结论", widget=forms.Textarea(attrs={"rows": 5}))
    recommended_action = forms.CharField(label="建议下一步", widget=forms.Textarea(attrs={"rows": 4}))
    confidence = forms.DecimalField(label="置信度（0 到 1）", min_value=0, max_value=1, max_digits=5, decimal_places=4)
    evidence_ref = forms.ChoiceField(label="确切证据", choices=())
    evidence_note = forms.CharField(label="证据说明", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, actor=None, evidence_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        candidates = Product.objects.filter(
            product_status=Product.ProductStatus.ACTIVE
        ).order_by("product_code")
        allowed_ids = []
        if actor and actor.is_authenticated:
            for product in candidates:
                decision = resolve_authorization(
                    principal=actor,
                    acting_role=actor.role,
                    action=PermissionGrant.Action.EDIT,
                    scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                    product=product,
                )
                if decision.allowed:
                    allowed_ids.append(product.pk)
        self.fields["product"].queryset = candidates.filter(pk__in=allowed_ids)
        self.fields["evidence_ref"].choices = list(evidence_choices)
