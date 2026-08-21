from __future__ import annotations

import uuid

from django import forms

from contentops.models import ReviewDecision
from integrations.publishing import PublicationMode
from releasegate.models import ChannelAccount, Publication, RuntimeEnvironment


class CommandVersionForm(forms.Form):
    command_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_state_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, state_version: int, **kwargs):
        initial = kwargs.setdefault("initial", {})
        initial.setdefault("command_id", uuid.uuid4())
        initial.setdefault("expected_state_version", state_version)
        super().__init__(*args, **kwargs)


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

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("mode")
        if mode == PublicationMode.MANUAL and not (
            cleaned.get("external_url") or cleaned.get("external_publication_id")
        ):
            raise forms.ValidationError("已发布内容网址或平台内容 ID 至少填写一项。")
        if mode in {PublicationMode.API, PublicationMode.BROWSER} and (
            cleaned.get("external_url") or cleaned.get("external_publication_id")
        ):
            raise forms.ValidationError("API/浏览器方式的发布结果只能由受控运行层返回，不能手工填写。")
        return cleaned


class CompleteTaskForm(CommandVersionForm):
    pass
