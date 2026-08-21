from __future__ import annotations

import json

from django import forms
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from integrations.connectors.types import Platform

from products.models import (
    ClaimMatrixVersion,
    ControlledEvidenceItemVersion,
    EvidenceLibraryVersion,
    ObjectiveProfileVersion,
    Product,
    ProductClaimVersion,
    ProductProfileAssetLink,
    ProductProfileVersion,
)
from releasegate.models import AccountEnvironmentBinding, CapabilityState, ChannelAccount, RuntimeEnvironment


class JSONField(forms.CharField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", forms.Textarea(attrs={"rows": 3}))
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        if value in self.empty_values:
            return {} if not self.required else None
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise forms.ValidationError("请填写有效的 JSON，例如 {\"重点\": \"曝光\"}。") from error


class JSONListField(JSONField):
    def to_python(self, value):
        parsed = super().to_python(value)
        if parsed is None:
            return parsed
        if not isinstance(parsed, list):
            raise forms.ValidationError("这里需要 JSON 列表，例如 [\"曝光\", \"SEO\"]。")
        return parsed


class ObjectiveProfileForm(forms.Form):
    objective_key = forms.CharField(label="目标配置名称", max_length=100)
    primary_objectives = JSONListField(label="主要目标", initial='["曝光", "SEO", "GEO", "账号访问"]')
    secondary_objectives = JSONListField(label="次要目标", initial='["互动"]')
    retained_metrics = JSONListField(label="只保留观察的商业指标", initial='["产品浏览", "购买", "收入"]')
    priority_rules = JSONField(label="优先级规则", initial='{"第一优先": "安全与合规"}')
    strategy_boundaries = JSONField(label="策略边界", initial='{"商业结果不进入外部需求": true}')


class ClaimMatrixForm(forms.Form):
    claim_key = forms.CharField(label="声明稳定名称", max_length=120)
    claim_type = forms.ChoiceField(label="声明类型", choices=ProductClaimVersion.ClaimType.choices)
    evidence_level = forms.ChoiceField(label="证据等级", choices=ProductClaimVersion.EvidenceLevel.choices)
    platform_code = forms.CharField(label="限定平台（可空）", max_length=64, required=False)
    wording = forms.CharField(label="允许或禁止的具体说法", widget=forms.Textarea(attrs={"rows": 3}))


class EvidenceLibraryForm(forms.Form):
    evidence_key = forms.CharField(label="证据稳定名称", max_length=120)
    title = forms.CharField(label="证据标题", max_length=240)
    provider = forms.CharField(label="来源机构或网站", max_length=120)
    external_url = forms.URLField(label="外部链接（不上传文件）", max_length=2048)
    version_reference = forms.CharField(label="版本或日期标识", max_length=255, required=False)
    summary = forms.CharField(label="摘要", required=False, widget=forms.Textarea(attrs={"rows": 3}))


class ProductProfileForm(forms.Form):
    objective_profile_version = forms.ModelChoiceField(label="封存的目标版本", queryset=ObjectiveProfileVersion.objects.none())
    claim_matrix_version = forms.ModelChoiceField(label="封存的声明矩阵", queryset=ClaimMatrixVersion.objects.none())
    evidence_library_version = forms.ModelChoiceField(label="封存的证据库", queryset=EvidenceLibraryVersion.objects.none())
    audience = JSONField(label="受众", initial='{"主要受众": "美国消费者"}')
    core_value_proposition = forms.CharField(label="核心价值主张", widget=forms.Textarea(attrs={"rows": 3}))
    brand_voice = JSONField(label="品牌语气", initial='{"语气": ["清楚", "可信"]}')
    product_facts = JSONField(label="产品事实", initial="{}")
    prohibited_expressions = JSONListField(label="禁用表达", initial='["治疗", "治愈"]')

    def __init__(self, *args, product: Product, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["objective_profile_version"].queryset = ObjectiveProfileVersion.objects.filter(
            sealed_at__isnull=False
        ).order_by("-created_at")
        self.fields["claim_matrix_version"].queryset = ClaimMatrixVersion.objects.filter(
            product=product, sealed_at__isnull=False
        ).order_by("-version_number")
        self.fields["evidence_library_version"].queryset = EvidenceLibraryVersion.objects.filter(
            product=product, sealed_at__isnull=False
        ).order_by("-version_number")


class ProfileAssetLinkForm(forms.Form):
    profile = forms.ModelChoiceField(label="未封存的产品档案", queryset=ProductProfileVersion.objects.none())
    evidence = forms.ModelChoiceField(label="外部证据链接", queryset=ControlledEvidenceItemVersion.objects.none())
    asset_kind = forms.ChoiceField(
        label="用途",
        choices=[
            (ProductProfileAssetLink.AssetKind.LITERATURE_LINK, "文献链接"),
            (ProductProfileAssetLink.AssetKind.PRODUCT_IMAGE_LINK, "产品图片链接"),
            (ProductProfileAssetLink.AssetKind.VISUAL_SPEC, "视觉规范链接"),
            (ProductProfileAssetLink.AssetKind.LOGO, "Logo 链接"),
            (ProductProfileAssetLink.AssetKind.CONTROLLED_EVIDENCE, "受控证据"),
        ],
    )

    def __init__(self, *args, product: Product, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["profile"].queryset = ProductProfileVersion.objects.filter(
            product=product, sealed_at__isnull=True
        ).order_by("-version_number")
        self.fields["evidence"].queryset = ControlledEvidenceItemVersion.objects.filter(
            product=product
        ).order_by("-created_at")


class SealProfileForm(forms.Form):
    profile = forms.ModelChoiceField(label="要封存并启用的草稿", queryset=ProductProfileVersion.objects.none())

    def __init__(self, *args, product: Product, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["profile"].queryset = ProductProfileVersion.objects.filter(
            product=product, sealed_at__isnull=True
        ).order_by("-version_number")


class ChannelAccountForm(forms.ModelForm):
    platform_code = forms.ChoiceField(
        label="平台",
        choices=[
            (Platform.PINTEREST.value, "Pinterest"),
            (Platform.QUORA.value, "Quora"),
            (Platform.TIKTOK.value, "TikTok"),
            (Platform.SHOPIFY.value, "Shopify / 独立站"),
            (Platform.GOOGLE_SEARCH.value, "Google Search"),
            (Platform.GOOGLE_SEARCH_CONSOLE.value, "Google Search Console"),
            (Platform.GOOGLE_ANALYTICS_4.value, "Google Analytics 4"),
        ],
    )

    class Meta:
        model = ChannelAccount
        fields = ["platform_code", "account_code", "external_account_ref", "display_name", "status"]
        labels = {
            "platform_code": "平台代码", "account_code": "内部账号代码",
            "external_account_ref": "平台账号 ID", "display_name": "显示名称", "status": "状态",
        }


class RuntimeEnvironmentForm(forms.ModelForm):
    class Meta:
        model = RuntimeEnvironment
        fields = [
            "environment_code", "environment_type", "identity_namespace",
            "database_namespace", "object_storage_namespace", "status",
        ]
        labels = {
            "environment_code": "环境代码", "environment_type": "环境类型",
            "identity_namespace": "身份空间（只填名称）", "database_namespace": "数据库空间（只填名称）",
            "object_storage_namespace": "链接存储说明（V1 可填 DISABLED）", "status": "状态",
        }


class BindingForm(forms.Form):
    channel_account = forms.ModelChoiceField(label="渠道账号", queryset=ChannelAccount.objects.none())
    runtime_environment = forms.ModelChoiceField(label="运行环境", queryset=RuntimeEnvironment.objects.none())
    identity_reference = forms.CharField(label="身份引用名称（不能填密码或密钥）", max_length=255)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["channel_account"].queryset = ChannelAccount.objects.filter(
            status=ChannelAccount.Status.ACTIVE
        ).order_by("platform_code", "account_code")
        self.fields["runtime_environment"].queryset = RuntimeEnvironment.objects.filter(
            status=RuntimeEnvironment.Status.ACTIVE
        ).order_by("environment_code")


class CapabilityForm(forms.Form):
    binding = forms.ModelChoiceField(label="账号与环境绑定", queryset=AccountEnvironmentBinding.objects.none())
    capability_code = forms.CharField(label="能力代码", max_length=64, initial=CapabilityState.MANUAL_PUBLISH)
    state = forms.ChoiceField(label="当前能力", choices=CapabilityState.State.choices)
    reason = forms.CharField(label="原因", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        now = timezone.now()
        newer = AccountEnvironmentBinding.objects.filter(
            channel_account_id=OuterRef("channel_account_id"),
            runtime_environment_id=OuterRef("runtime_environment_id"),
            binding_version__gt=OuterRef("binding_version"),
        )
        self.fields["binding"].queryset = AccountEnvironmentBinding.objects.select_related(
            "channel_account", "runtime_environment"
        ).annotate(has_newer=Exists(newer)).filter(
            has_newer=False,
            status=AccountEnvironmentBinding.Status.ACTIVE,
            valid_from__lte=now,
            channel_account__status=ChannelAccount.Status.ACTIVE,
            runtime_environment__status=RuntimeEnvironment.Status.ACTIVE,
        ).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now)).order_by(
            "channel_account__account_code", "runtime_environment__environment_code"
        )
