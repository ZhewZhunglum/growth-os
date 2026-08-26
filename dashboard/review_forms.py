from __future__ import annotations

import uuid

from django import forms
from django.utils.translation import get_language

from contentops.models import ReviewDecision
from integrations.publishing import PublicationMode
from releasegate.models import ChannelAccount, Publication, RuntimeEnvironment


def _is_english() -> bool:
    return str(get_language() or "zh-hans").lower().startswith("en")


REVIEW_FORM_TEXT = {
    "decision": ("审核结论", "Review decision"),
    "rationale": ("审核说明", "Review notes"),
    "channel_account": ("发布账号", "Publishing account"),
    "runtime_environment": ("运行环境", "Runtime environment"),
    "mode": ("执行方式", "Publishing method"),
    "external_url": ("已发布内容网址", "Published content URL"),
    "external_publication_id": ("平台内容 ID", "Platform content ID"),
    "confirmed": (
        "我已核对账号、内容和门禁，并确认执行所选发布方式",
        "I checked the account, content, and release gate, and confirm the selected publishing method",
    ),
}

REVIEW_FORM_HELP = {
    "rationale": (
        "请用自然语言说明为什么通过，或具体需要修改什么。",
        "Explain in plain language why this passes or exactly what needs to change.",
    ),
    "external_url": (
        "选择人工发布时，网址或平台内容 ID 至少填写一项。",
        "For manual publishing, provide either a URL or a platform content ID.",
    ),
}


def _localize_form(form: forms.Form) -> None:
    if not _is_english():
        return
    for name, field in form.fields.items():
        if name in REVIEW_FORM_TEXT:
            field.label = REVIEW_FORM_TEXT[name][1]
        if name in REVIEW_FORM_HELP:
            field.help_text = REVIEW_FORM_HELP[name][1]


class CommandVersionForm(forms.Form):
    command_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_state_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, state_version: int, **kwargs):
        initial = kwargs.setdefault("initial", {})
        initial.setdefault("command_id", uuid.uuid4())
        initial.setdefault("expected_state_version", state_version)
        super().__init__(*args, **kwargs)
        _localize_form(self)


class ReviewDecisionForm(CommandVersionForm):
    decision = forms.ChoiceField(
        label="审核结论",
        choices=(
            (ReviewDecision.Decision.APPROVED, "通过，进入发布门禁"),
            (ReviewDecision.Decision.CHANGES_REQUESTED, "需要修改，退回人工返工"),
        ),
    )
    rationale = forms.CharField(
        label="审核说明",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="请用自然语言说明为什么通过，或具体需要修改什么。",
    )

    def __init__(self, *args, **kwargs):
        owner_self_approval = kwargs.pop("owner_self_approval", False)
        super().__init__(*args, **kwargs)
        if owner_self_approval:
            self.fields["decision"].choices = (
                (ReviewDecision.Decision.APPROVED, "Owner 最终批准（已审计）"),
            )
            self.fields["decision"].help_text = "Owner 可批准自己提交的内容；本次批准会保留独立审计记录。"
            self.fields["rationale"].label = "批准说明"
            self.fields["rationale"].help_text = "请简要记录为什么批准进入发布检查。"
        if _is_english():
            if owner_self_approval:
                self.fields["decision"].choices = (
                    (ReviewDecision.Decision.APPROVED, "Owner final approval (audited)"),
                )
                self.fields["decision"].help_text = (
                    "An Owner may approve their own submission; this approval is recorded separately."
                )
                self.fields["rationale"].label = "Approval note"
                self.fields["rationale"].help_text = "Briefly record why this may continue to release checks."
            else:
                self.fields["decision"].choices = (
                    (ReviewDecision.Decision.APPROVED, "Approve and continue to release checks"),
                    (ReviewDecision.Decision.CHANGES_REQUESTED, "Request changes and return for revision"),
                )


class ReleaseGateForm(CommandVersionForm):
    channel_account = forms.ModelChoiceField(
        queryset=ChannelAccount.objects.none(),
        label="发布账号",
        empty_label="请选择账号",
    )
    runtime_environment = forms.ModelChoiceField(
        queryset=RuntimeEnvironment.objects.none(),
        label="运行环境",
        empty_label="请选择环境",
    )

    def __init__(self, *args, accounts, environments, state_version: int, **kwargs):
        super().__init__(*args, state_version=state_version, **kwargs)
        self.fields["channel_account"].queryset = accounts
        self.fields["runtime_environment"].queryset = environments
        if _is_english():
            self.fields["channel_account"].empty_label = "Select an account"
            self.fields["runtime_environment"].empty_label = "Select an environment"


class StopPublicationForm(CommandVersionForm):
    reason = forms.CharField(
        label="停止发布原因",
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="不会删除任务、审核或门禁历史；停止后旧门禁不可继续使用。",
    )
    confirmed = forms.BooleanField(
        label="我确认停止这项发布工作",
        required=True,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if _is_english():
            self.fields["reason"].label = "Reason for stopping publication"
            self.fields["reason"].help_text = (
                "The task, review and gate history stay intact; the old gate cannot be reused."
            )
            self.fields["confirmed"].label = "I confirm that this publication should stop"


class ReturnToInlineContentForm(CommandVersionForm):
    reason = forms.CharField(
        label="退回制作原因",
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="保留旧链接、提交、审核和门禁；下一版必须提交完整正文。",
    )
    confirmed = forms.BooleanField(
        label="我确认退回制作完整内容",
        required=True,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if _is_english():
            self.fields["reason"].label = "Reason for returning to production"
            self.fields["reason"].help_text = (
                "The old link, submission, review and gate stay intact; the next version must contain the full text."
            )
            self.fields["confirmed"].label = "I confirm return for complete inline content"


class PublicationProofForm(forms.Form):
    command_id = forms.UUIDField(widget=forms.HiddenInput)
    publication = forms.ModelChoiceField(
        queryset=Publication.objects.none(),
        widget=forms.HiddenInput,
    )
    mode = forms.ChoiceField(
        label="执行方式",
        choices=(
            (PublicationMode.MANUAL, "人工发布（推荐；发布后登记网址或内容 ID）"),
            (PublicationMode.API, "平台 API（当前默认关闭）"),
            (PublicationMode.BROWSER, "受控浏览器（当前默认关闭）"),
        ),
        initial=PublicationMode.MANUAL,
    )
    external_url = forms.URLField(
        label="已发布内容网址",
        required=False,
        max_length=1024,
        help_text="选择人工发布时，网址或平台内容 ID 至少填写一项。",
    )
    external_publication_id = forms.CharField(
        label="平台内容 ID",
        required=False,
        max_length=255,
    )
    confirmed = forms.BooleanField(
        label="我已核对账号、内容和门禁，并确认执行所选发布方式",
        required=True,
    )

    def __init__(self, *args, publications, initial_publication=None, **kwargs):
        initial = kwargs.setdefault("initial", {})
        initial.setdefault("command_id", uuid.uuid4())
        if initial_publication is not None:
            initial.setdefault("publication", initial_publication)
        super().__init__(*args, **kwargs)
        self.fields["publication"].queryset = publications
        if _is_english():
            self.fields["mode"].choices = (
                (PublicationMode.MANUAL, "Manual publishing (recommended; record the URL or content ID afterward)"),
                (PublicationMode.API, "Platform API (off by default)"),
                (PublicationMode.BROWSER, "Controlled browser (off by default)"),
            )
        _localize_form(self)

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("mode")
        if mode == PublicationMode.MANUAL and not (
            cleaned.get("external_url") or cleaned.get("external_publication_id")
        ):
            raise forms.ValidationError(
                "Provide either the published URL or the platform content ID."
                if _is_english()
                else "已发布内容网址或平台内容 ID 至少填写一项。"
            )
        if mode in {PublicationMode.API, PublicationMode.BROWSER} and (
            cleaned.get("external_url") or cleaned.get("external_publication_id")
        ):
            raise forms.ValidationError(
                "API or browser results must come from the controlled runtime and cannot be entered manually."
                if _is_english()
                else "API/浏览器方式的发布结果只能由受控运行层返回，不能手工填写。"
            )
        return cleaned


class CompleteTaskForm(CommandVersionForm):
    pass
