from __future__ import annotations

import uuid
from datetime import timedelta

from django import forms
from django.utils import timezone

from integrations.connectors.types import Platform
from products.models import Product
from releasegate.models import CapabilityState, ChannelAccount


class CommandForm(forms.Form):
    command_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)


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

    def __init__(self, *args, products=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = products if products is not None else Product.objects.none()
        if not self.is_bound:
            end = timezone.localtime().replace(second=0, microsecond=0)
            start = end - timedelta(days=7)
            self.initial.setdefault("window_start", start.strftime("%Y-%m-%dT%H:%M"))
            self.initial.setdefault("window_end", end.strftime("%Y-%m-%dT%H:%M"))

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("window_start"), cleaned.get("window_end")
        if start and end and end <= start:
            raise forms.ValidationError("结束时间必须晚于开始时间。")
        return cleaned


class ManualEvidenceForm(CommandForm):
    collected_at = forms.DateTimeField(widget=forms.HiddenInput, initial=timezone.now)
    external_url = forms.URLField(
        label="原内容链接（链接和内容 ID 至少填一个）", required=False, max_length=4_096
    )
    external_content_id = forms.CharField(label="平台内容 ID", required=False, max_length=1_000)
    title = forms.CharField(label="标题", required=False, max_length=5_000)
    content_text = forms.CharField(
        label="看到的正文/摘要",
        required=False,
        max_length=250_000,
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("external_url") and not cleaned.get("external_content_id"):
            raise forms.ValidationError("请填写原内容链接或平台内容 ID。")
        if not cleaned.get("title") and not cleaned.get("content_text"):
            raise forms.ValidationError("请填写标题或正文摘要。")
        return cleaned


class CSVTextForm(CommandForm):
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


class TransitionForm(CommandForm):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    to_state = forms.CharField(widget=forms.HiddenInput)
    reason = forms.CharField(
        label="决定理由",
        max_length=2_000,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "用一句大白话说明为什么这样决定"}),
    )


class ChannelPlanForm(CommandForm):
    platform = forms.ChoiceField(
        label="执行平台", choices=[(platform.value, platform.value.replace("_", " ")) for platform in Platform]
    )
    channel_account = forms.ModelChoiceField(
        label="具体平台账号", queryset=ChannelAccount.objects.none(), empty_label="请选择账号"
    )
    plan_date = forms.DateField(
        label="执行日期", widget=forms.DateInput(attrs={"type": "date"}), initial=timezone.localdate
    )
    task_title = forms.CharField(label="任务标题", max_length=240)
    task_description = forms.CharField(
        label="要做什么", max_length=5_000, widget=forms.Textarea(attrs={"rows": 3})
    )
    environment_code = forms.CharField(
        label="运行环境代码", max_length=100, help_text="例如 staging；必须与该账号当前绑定一致。"
    )
    capability_code = forms.CharField(
        label="能力代码", max_length=64, initial=CapabilityState.MANUAL_PUBLISH
    )

    def __init__(self, *args, accounts=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["channel_account"].queryset = (
            accounts if accounts is not None else ChannelAccount.objects.none()
        )

    def clean(self):
        cleaned = super().clean()
        platform = cleaned.get("platform")
        account = cleaned.get("channel_account")
        if account and platform and account.platform_code != platform:
            raise forms.ValidationError("所选账号不属于这个平台。")
        return cleaned


class CompileTaskForm(CommandForm):
    task_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
