from __future__ import annotations

import json

from django import forms
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from dailyops.forms import channel_account_label, platform_label
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


def _is_english(language_code: str | None) -> bool:
    return str(language_code or "").lower().startswith("en")


def _runtime_environment_choice_label(
    environment: RuntimeEnvironment,
    *,
    english: bool,
) -> str:
    if environment.environment_type == RuntimeEnvironment.EnvironmentType.PRODUCTION:
        friendly_name = "Production" if english else "正式环境"
    elif environment.environment_code == "local-dogfood" or environment.environment_code.startswith("local-"):
        friendly_name = "Local practice" if english else "本地练习"
    else:
        friendly_name = "Test environment" if english else "测试环境"
    return f"{friendly_name} · {environment.environment_code}"


def _binding_choice_label(binding: AccountEnvironmentBinding, *, english: bool) -> str:
    account = channel_account_label(binding.channel_account, english=english)
    environment = _runtime_environment_choice_label(binding.runtime_environment, english=english)
    version = f"version {binding.binding_version}" if english else f"版本 {binding.binding_version}"
    return f"{account} → {environment} · {version}"


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
    status = forms.ChoiceField(
        label="使用状态",
        choices=[(ChannelAccount.Status.ACTIVE, "使用中")],
        initial=ChannelAccount.Status.ACTIVE,
        widget=forms.HiddenInput(),
    )

    class Meta:
        model = ChannelAccount
        fields = ["platform_code", "account_code", "external_account_ref", "display_name", "status"]
        labels = {
            "platform_code": "平台", "account_code": "内部账号名称",
            "external_account_ref": "平台账号 ID", "display_name": "页面显示名称", "status": "使用状态",
        }

    def __init__(self, *args, language_code: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if str(language_code or "").lower().startswith("en"):
            labels = {
                "platform_code": "Platform",
                "account_code": "Internal account name",
                "external_account_ref": "Platform account ID",
                "display_name": "Display name",
                "status": "Usage status",
            }
            for name, label in labels.items():
                self.fields[name].label = label
            self.fields["platform_code"].choices = [
                (platform.value, platform_label(platform, english=True)) for platform in Platform
            ]
            self.fields["status"].choices = [
                (ChannelAccount.Status.ACTIVE, "Active"),
            ]


class RuntimeEnvironmentForm(forms.ModelForm):
    environment_type = forms.ChoiceField(
        label="使用场景",
        choices=[
            (RuntimeEnvironment.EnvironmentType.STAGING, "测试环境"),
            (RuntimeEnvironment.EnvironmentType.PRODUCTION, "正式环境"),
        ],
    )
    status = forms.ChoiceField(
        label="使用状态",
        choices=[(RuntimeEnvironment.Status.ACTIVE, "使用中")],
        initial=RuntimeEnvironment.Status.ACTIVE,
        widget=forms.HiddenInput(),
    )

    class Meta:
        model = RuntimeEnvironment
        fields = [
            "environment_code", "environment_type", "identity_namespace",
            "database_namespace", "object_storage_namespace", "status",
        ]
        labels = {
            "environment_code": "使用场景名称", "environment_type": "使用场景",
            "identity_namespace": "身份空间（只填名称）", "database_namespace": "数据库空间（只填名称）",
            "object_storage_namespace": "链接存储说明（V1 可填 DISABLED）", "status": "使用状态",
        }

    def __init__(self, *args, language_code: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if str(language_code or "").lower().startswith("en"):
            labels = {
                "environment_code": "Usage-context name",
                "environment_type": "Usage context",
                "identity_namespace": "Identity namespace (name only)",
                "database_namespace": "Database namespace (name only)",
                "object_storage_namespace": "Link-storage note (DISABLED is allowed)",
                "status": "Usage status",
            }
            for name, label in labels.items():
                self.fields[name].label = label
            self.fields["environment_type"].choices = [
                (RuntimeEnvironment.EnvironmentType.STAGING, "Test environment"),
                (RuntimeEnvironment.EnvironmentType.PRODUCTION, "Production"),
            ]
            self.fields["status"].choices = [
                (RuntimeEnvironment.Status.ACTIVE, "Active"),
            ]


class BindingForm(forms.Form):
    channel_account = forms.ModelChoiceField(label="渠道账号", queryset=ChannelAccount.objects.none())
    runtime_environment = forms.ModelChoiceField(label="使用场景", queryset=RuntimeEnvironment.objects.none())
    identity_reference = forms.CharField(
        label="连接标识名称（新连接必填；保留已有连接可留空）",
        help_text="只填写内部引用名称，不能填写密码或密钥。",
        max_length=255,
        required=False,
    )
    confirm_replace = forms.BooleanField(
        label="我确认：这个账号只保留所选使用场景；其他当前或预定连接会追加为“已撤销”记录。",
    )

    def __init__(self, *args, language_code: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.language_code = language_code
        english = _is_english(language_code)
        self.fields["channel_account"].queryset = ChannelAccount.objects.filter(
            status=ChannelAccount.Status.ACTIVE,
            platform_code__in=[platform.value for platform in Platform],
        ).order_by("platform_code", "account_code")
        self.fields["runtime_environment"].queryset = RuntimeEnvironment.objects.filter(
            status=RuntimeEnvironment.Status.ACTIVE
        ).order_by("environment_code")
        self.fields["channel_account"].label_from_instance = (
            lambda account: channel_account_label(account, english=english)
        )
        self.fields["runtime_environment"].label_from_instance = (
            lambda environment: _runtime_environment_choice_label(environment, english=english)
        )
        if english:
            self.fields["channel_account"].label = "Channel account"
            self.fields["runtime_environment"].label = "Usage context"
            self.fields["identity_reference"].label = (
                "Connection reference name (required for a new connection; blank keeps an existing one)"
            )
            self.fields["identity_reference"].help_text = (
                "Enter only an internal reference name, never a password or key."
            )
            self.fields["confirm_replace"].label = (
                "I confirm that this account will keep only the selected usage context; "
                "other current or scheduled connections will receive a Revoked record."
            )

    def clean(self):
        cleaned = super().clean()
        account = cleaned.get("channel_account")
        environment = cleaned.get("runtime_environment")
        reference = str(cleaned.get("identity_reference") or "").strip()
        if account is None or environment is None or reference:
            return cleaned

        now = timezone.now()
        latest = AccountEnvironmentBinding.objects.filter(
            channel_account=account,
            runtime_environment=environment,
        ).order_by("-binding_version", "-created_at", "-id").first()
        target_is_current = bool(
            latest
            and latest.status == AccountEnvironmentBinding.Status.ACTIVE
            and latest.valid_from <= now
            and (latest.valid_until is None or latest.valid_until > now)
        )
        if not target_is_current:
            self.add_error(
                "identity_reference",
                (
                    "A connection reference name is required for a new or reactivated connection."
                    if _is_english(self.language_code)
                    else "新增或重新启用连接时，必须填写连接标识名称。"
                ),
            )
        return cleaned


class CapabilityForm(forms.Form):
    binding = forms.ModelChoiceField(label="账号与使用场景", queryset=AccountEnvironmentBinding.objects.none())
    capability_code = forms.ChoiceField(
        label="功能",
        choices=[(CapabilityState.MANUAL_PUBLISH, "人工发布")],
        initial=CapabilityState.MANUAL_PUBLISH,
    )
    state = forms.ChoiceField(
        label="当前是否可用",
        choices=[
            (CapabilityState.State.OPEN, "可以使用"),
            (CapabilityState.State.CLOSED, "已关闭"),
            (CapabilityState.State.UNKNOWN, "尚未检查"),
        ],
    )
    reason = forms.CharField(label="说明（可不填）", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, language_code: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        english = _is_english(language_code)
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
            channel_account__platform_code__in=[platform.value for platform in Platform],
            runtime_environment__status=RuntimeEnvironment.Status.ACTIVE,
        ).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now)).order_by(
            "channel_account__account_code", "runtime_environment__environment_code"
        )
        self.fields["binding"].label_from_instance = (
            lambda binding: _binding_choice_label(binding, english=english)
        )
        if english:
            self.fields["binding"].label = "Account and usage context"
            self.fields["capability_code"].label = "Function"
            self.fields["capability_code"].choices = [
                (CapabilityState.MANUAL_PUBLISH, "Manual publishing")
            ]
            self.fields["state"].label = "Current availability"
            self.fields["state"].choices = [
                (CapabilityState.State.OPEN, "Available"),
                (CapabilityState.State.CLOSED, "Disabled"),
                (CapabilityState.State.UNKNOWN, "Not checked"),
            ]
            self.fields["reason"].label = "Note (optional)"
