from __future__ import annotations

import uuid
from datetime import timedelta

from django import forms
from django.utils import timezone

from dailyops.platform_detection import detect_platform
from dailyops.platforms import EXECUTION_PLATFORMS, EXECUTION_PLATFORM_VALUES
from integrations.connectors.types import Platform
from products.models import Product
from releasegate.models import ChannelAccount


def _is_english(language_code: str | None) -> bool:
    return str(language_code or "").lower().startswith("en")


PLATFORM_LABELS = {
    Platform.PINTEREST: ("Pinterest", "Pinterest"),
    Platform.QUORA: ("Quora", "Quora"),
    Platform.TIKTOK: ("TikTok", "TikTok"),
    Platform.SHOPIFY: ("Shopify / 独立站", "Shopify store"),
    Platform.GOOGLE_SEARCH: ("Google 搜索", "Google Search"),
    Platform.GOOGLE_SEARCH_CONSOLE: ("Google Search Console", "Google Search Console"),
    Platform.GOOGLE_ANALYTICS_4: ("Google Analytics 4", "Google Analytics 4"),
}


def platform_label(platform: Platform | str, *, english: bool = False) -> str:
    """Return a presentation-only platform name without changing stored values."""

    try:
        normalized = platform if isinstance(platform, Platform) else Platform(str(platform))
    except ValueError:
        return str(platform).replace("_", " ").title()
    labels = PLATFORM_LABELS[normalized]
    return labels[1] if english else labels[0]


def channel_account_label(account: ChannelAccount, *, english: bool = False) -> str:
    """Keep UUIDs out of visible account selectors while preserving exact IDs as values."""

    display_name = (account.display_name or account.account_code).strip()
    return f"{display_name} · {platform_label(account.platform_code, english=english)}"


def _platform_choices(*, english: bool, include_detect: bool = False):
    choices = [(platform.value, platform_label(platform, english=english)) for platform in Platform]
    if include_detect:
        choices.insert(0, ("", "Detect from link" if english else "让系统从链接识别"))
    return choices


class CommandForm(forms.Form):
    command_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)


class BatchDispositionForm(CommandForm):
    reason = forms.CharField(
        label="说明（可不填）",
        required=False,
        max_length=2_000,
        widget=forms.Textarea(
            attrs={"rows": 2, "placeholder": "例如：选错了研究问题，重新开始一轮"}
        ),
    )
    confirm = forms.BooleanField(
        label="我确认从最近工作隐藏；历史记录仍会保留",
        required=True,
    )

    def clean_reason(self):
        return str(self.cleaned_data.get("reason") or "").strip()


class DailyBatchForm(CommandForm):
    product = forms.ModelChoiceField(queryset=Product.objects.none(), label="产品")
    query = forms.CharField(
        label="今天要研究的关键词/问题",
        max_length=2_000,
        widget=forms.TextInput(attrs={"placeholder": "例如：下午注意力不集中怎么办"}),
    )
    window_start = forms.DateTimeField(
        label="研究时间从",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    window_end = forms.DateTimeField(
        label="研究时间到",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )

    def __init__(self, *args, products=None, language_code: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_english = _is_english(language_code)
        self.fields["product"].queryset = products if products is not None else Product.objects.none()
        if self.is_english:
            self.fields["product"].label = "Product"
            self.fields["query"].label = "What should we research today?"
            self.fields["query"].widget.attrs["placeholder"] = "For example: how to stay focused in the afternoon"
            self.fields["window_start"].label = "Research from"
            self.fields["window_end"].label = "Research until"
        if not self.is_bound:
            end = timezone.localtime().replace(second=0, microsecond=0)
            start = end - timedelta(days=7)
            self.initial.setdefault("window_start", start.strftime("%Y-%m-%dT%H:%M"))
            self.initial.setdefault("window_end", end.strftime("%Y-%m-%dT%H:%M"))

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("window_start"), cleaned.get("window_end")
        if start and end and end <= start:
            raise forms.ValidationError(
                "The end time must be later than the start time."
                if self.is_english
                else "结束时间必须晚于开始时间。"
            )
        return cleaned


class ManualEvidenceForm(CommandForm):
    platform = forms.ChoiceField(
        label="平台",
        required=False,
        choices=_platform_choices(english=False, include_detect=True),
    )
    reference = forms.CharField(
        label="链接或内容名称 / ID",
        required=False,
        max_length=4_096,
        widget=forms.TextInput(
            attrs={"placeholder": "粘贴 Pinterest、Quora、TikTok 等链接；没有链接时填内容 ID"}
        ),
    )
    collected_at = forms.DateTimeField(widget=forms.HiddenInput, initial=timezone.now)
    external_url = forms.URLField(
        label="原内容链接", required=False, max_length=4_096, widget=forms.HiddenInput
    )
    external_content_id = forms.CharField(
        label="平台内容 ID", required=False, max_length=1_000, widget=forms.HiddenInput
    )
    title = forms.CharField(
        label="标题（可选）",
        required=False,
        max_length=5_000,
        widget=forms.TextInput(attrs={"placeholder": "不填也可以，系统会先用链接或 ID 作为名称"}),
    )
    content_text = forms.CharField(
        label="补充说明（可选）",
        required=False,
        max_length=250_000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(
        self,
        *args,
        expected_platform: Platform | None = None,
        language_code: str | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.is_english = _is_english(language_code)
        self.expected_platform = expected_platform
        if self.is_english:
            self.fields["platform"].label = "Platform"
            self.fields["platform"].choices = _platform_choices(
                english=True, include_detect=True
            )
            self.fields["reference"].label = "Link or content name / ID"
            self.fields["reference"].widget.attrs["placeholder"] = (
                "Paste a Pinterest, Quora, TikTok, or other supported link"
            )
            self.fields["title"].label = "Title (optional)"
            self.fields["content_text"].label = "Notes (optional)"
        if expected_platform is not None:
            self.initial.setdefault("platform", expected_platform.value)

    def clean(self):
        cleaned = super().clean()
        reference = (cleaned.get("reference") or "").strip()
        external_url = (cleaned.get("external_url") or "").strip()
        external_content_id = (cleaned.get("external_content_id") or "").strip()
        detection = detect_platform(reference) if reference else None
        if reference:
            if detection and detection.is_url:
                external_url = reference if "://" in reference else f"https://{reference}"
                external_content_id = ""
            else:
                external_content_id = reference
                external_url = ""
        if not external_url and not external_content_id:
            raise forms.ValidationError(
                "Paste a link or enter a content name / ID."
                if self.is_english
                else "请粘贴一个链接，或者填写内容名称 / ID。"
            )

        explicit_value = cleaned.get("platform") or (
            self.expected_platform.value if self.expected_platform is not None else ""
        )
        explicit_platform = Platform(explicit_value) if explicit_value else None
        detected_platform = detection.platform if detection else None
        if detected_platform and explicit_platform and detected_platform is not explicit_platform:
            detected_label = platform_label(detected_platform, english=self.is_english)
            explicit_label = platform_label(explicit_platform, english=self.is_english)
            raise forms.ValidationError(
                (
                    f"This link belongs to {detected_label}, not the selected "
                    f"{explicit_label}."
                )
                if self.is_english
                else f"这个链接属于 {detected_label}，与你选择的 {explicit_label} 不一致。"
            )
        resolved_platform = detected_platform or explicit_platform
        if resolved_platform is None:
            raise forms.ValidationError(
                "The platform cannot be detected from this entry. Choose it manually."
                if self.is_english
                else "系统无法从这条内容识别平台，请手动选择平台。"
            )

        cleaned["platform"] = resolved_platform
        cleaned["external_url"] = external_url
        cleaned["external_content_id"] = external_content_id
        if not (cleaned.get("title") or "").strip():
            cleaned["title"] = reference or external_url or external_content_id
        return cleaned


class EvidenceCorrectionForm(ManualEvidenceForm):
    reason = forms.CharField(
        label="更正说明（可不填）",
        required=False,
        max_length=2_000,
        widget=forms.Textarea(
            attrs={"rows": 2, "placeholder": "例如：原链接粘贴错误，改为正确版本"}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.is_english:
            self.fields["reason"].label = "Reason for correction"
            self.fields["reason"].widget.attrs["placeholder"] = (
                "For example: replace the incorrect link with the correct version"
            )

    def clean_reason(self):
        return str(self.cleaned_data.get("reason") or "").strip()


class CSVTextForm(CommandForm):
    platform = forms.ChoiceField(
        label="平台",
        choices=_platform_choices(english=False),
    )
    csv_text = forms.CharField(
        label="CSV 文本（直接粘贴，不上传文件）",
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "placeholder": (
                    "url,title,content_text,collected_at\n"
                    "https://example.com/post,示例标题,示例正文,2026-08-21T09:00:00+08:00"
                ),
            }
        ),
    )

    def __init__(
        self,
        *args,
        expected_platform: Platform | None = None,
        language_code: str | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.is_english = _is_english(language_code)
        self.expected_platform = expected_platform
        if self.is_english:
            self.fields["platform"].label = "Platform"
            self.fields["platform"].choices = _platform_choices(english=True)
            self.fields["csv_text"].label = "Paste CSV text (no file upload)"
        if expected_platform is not None:
            self.fields["platform"].required = False
            self.initial.setdefault("platform", expected_platform.value)

    def clean_platform(self):
        value = self.cleaned_data.get("platform") or (
            self.expected_platform.value if self.expected_platform is not None else ""
        )
        if not value:
            raise forms.ValidationError(
                "Choose the platform for this CSV."
                if self.is_english
                else "请选择 CSV 属于哪个平台。"
            )
        platform = Platform(value)
        if self.expected_platform is not None and platform is not self.expected_platform:
            raise forms.ValidationError(
                "The CSV platform does not match this entry point."
                if self.is_english
                else "CSV 平台与当前入口不一致。"
            )
        return platform


class TransitionForm(CommandForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    to_state = forms.CharField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="决定说明（可不填）",
        required=False,
        max_length=2_000,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "用一句大白话说明为什么这样决定"}),
    )

    def clean_reason(self):
        return str(self.cleaned_data.get("reason") or "").strip()


class ChannelPlanForm(CommandForm):
    platform = forms.ChoiceField(
        label="执行平台",
        choices=(),
    )
    channel_account = forms.ModelChoiceField(
        label="具体平台账号",
        queryset=ChannelAccount.objects.none(),
        empty_label="请选择账号",
        required=False,
    )
    plan_date = forms.DateField(
        label="执行日期",
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        initial=timezone.localdate,
    )
    task_title = forms.CharField(label="任务标题", max_length=240)
    task_description = forms.CharField(
        label="要做什么", max_length=5_000, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(
        self,
        *args,
        accounts=None,
        excluded_platforms=(),
        language_code: str | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.is_english = _is_english(language_code)
        account_queryset = accounts if accounts is not None else ChannelAccount.objects.none()
        excluded_platform_values = {
            platform.value if isinstance(platform, Platform) else str(platform)
            for platform in excluded_platforms
        }
        configured_execution_accounts = [
            account
            for account in account_queryset
            if account.platform_code in EXECUTION_PLATFORM_VALUES
        ]
        account_list = [
            account
            for account in configured_execution_accounts
            if account.platform_code not in excluded_platform_values
        ]
        self.fields["channel_account"].queryset = ChannelAccount.objects.filter(
            pk__in=[account.pk for account in account_list]
        ).order_by("platform_code", "display_name", "account_code")
        self.fields["channel_account"].label_from_instance = lambda account: channel_account_label(
            account, english=self.is_english
        )
        if self.is_english:
            self.fields["platform"].label = "Platform"
            self.fields["channel_account"].label = "Account"
            self.fields["channel_account"].empty_label = "Choose an account"
            self.fields["plan_date"].label = "Work date"
            self.fields["task_title"].label = "Task title"
            self.fields["task_description"].label = "What should be done?"
            self.fields["platform"].error_messages["invalid_choice"] = (
                "That platform is already planned or is analysis-only; continue the existing work."
            )
        else:
            self.fields["platform"].error_messages["invalid_choice"] = (
                "这个平台在本轮已经安排过（已取消的安排也保留历史），"
                "或它只用于数据分析；如需重做，请新开一轮 Daily Operations。"
            )

        self._accounts_by_platform: dict[str, list[ChannelAccount]] = {}
        for account in account_list:
            self._accounts_by_platform.setdefault(account.platform_code, []).append(account)

        self.fields["platform"].choices = [
            (platform.value, platform_label(platform, english=self.is_english))
            for platform in EXECUTION_PLATFORMS
            if platform.value in self._accounts_by_platform
        ]
        self.has_platform_choices = bool(self.fields["platform"].choices)
        if self.has_platform_choices:
            self.no_platform_reason = ""
        elif configured_execution_accounts:
            self.no_platform_reason = "EXHAUSTED"
        else:
            # The queryset is already permission-filtered by the server.  Do
            # not pretend platforms are exhausted when this principal simply
            # has no configured/authorized execution account.
            self.no_platform_reason = "UNAVAILABLE"

        # The normal V1 case is one configured account per platform.  In that
        # case the server derives the exact account after the platform is
        # chosen, so a user cannot accidentally select a cross-platform pair.
        if self._accounts_by_platform and all(
            len(platform_accounts) == 1
            for platform_accounts in self._accounts_by_platform.values()
        ):
            self.fields["channel_account"].widget = forms.HiddenInput()

        if not self.is_bound and len(self.fields["platform"].choices) == 1:
            self.initial.setdefault("platform", self.fields["platform"].choices[0][0])

    def clean(self):
        cleaned = super().clean()
        platform = cleaned.get("platform")
        account = cleaned.get("channel_account")
        if not platform:
            return cleaned

        matching_accounts = self._accounts_by_platform.get(platform, [])
        if account and account.platform_code != platform:
            self.add_error(
                "channel_account",
                "The selected account does not match the platform."
                if self.is_english
                else "所选账号与执行平台不匹配。",
            )
            return cleaned
        if account:
            return cleaned

        if len(matching_accounts) == 1:
            cleaned["channel_account"] = matching_accounts[0]
        elif not matching_accounts:
            self.add_error(
                "platform",
                "No usable account is configured for this platform."
                if self.is_english
                else "这个平台还没有可用账号，请先完成账号配置。",
            )
        else:
            self.add_error(
                "channel_account",
                "Choose which account to use for this work."
                if self.is_english
                else "这个平台有多个账号，请选择本次使用的账号。",
            )
        return cleaned


class CompileTaskForm(CommandForm):
    task_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
