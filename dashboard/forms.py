from __future__ import annotations

import uuid

from django import forms
from django.core import signing
from django.utils.translation import get_language

from accounts.models import Principal
from core.ids import uuid7
from products.models import ProductProfileVersion
from workflow.models import TaskCheckRun, TaskContractVersion


CHECK_CHOICES = (
    ("", "请选择结果"),
    (TaskCheckRun.Result.PASS, "通过（PASS）"),
    (TaskCheckRun.Result.BLOCKED, "阻塞（BLOCKED）"),
)

CHECK_CHOICES_EN = (
    ("", "Select a result"),
    (TaskCheckRun.Result.PASS, "Pass"),
    (TaskCheckRun.Result.BLOCKED, "Not ready"),
)


def _is_english() -> bool:
    return str(get_language() or "zh-hans").lower().startswith("en")


FORM_TEXT = {
    "product_profile_version": ("产品配置版本", "Product profile version"),
    "contract_version": ("任务合同版本", "Task contract version"),
    "title": ("任务名称", "Task name"),
    "description": ("为什么做 / 任务说明", "Purpose / task instructions"),
    "assignee": ("分配给哪位执行负责人", "Assign to"),
    "external_url": ("本次交付链接", "Delivery link"),
    "submission_note": ("交付说明", "Delivery note"),
}

FORM_HELP = {
    "description": (
        "请用自然语言说明背景和目标；具体 DoR/DoD 来自所选合同。",
        "Explain the context and goal in plain language. Readiness and delivery checks come from the selected contract.",
    ),
    "external_url": (
        "必填。请填写可供审核和发布人员打开的内容链接。链接变化时需提交新版本。",
        "Required. Add a link that reviewers and publishers can open. Submit a new version whenever the link changes.",
    ),
}

CLASS_FORM_TEXT = {
    "CancelTaskForm": {
        "reason": ("取消原因", "Reason for cancellation"),
        "confirm": ("确认取消这份草稿", "I confirm that this draft should be cancelled"),
    },
    "WithdrawSubmissionForm": {
        "reason": ("撤回原因", "Reason for withdrawal"),
        "confirm": ("确认撤回并重新修改", "I confirm that I want to withdraw and revise this submission"),
    },
}

CLASS_FORM_HELP = {
    "CancelTaskForm": {
        "reason": (
            "任务不会被删除；系统会保留审计记录，并从 Today 主列表隐藏。",
            "The task is not deleted. Its audit history remains, and it is hidden from the main Today list.",
        ),
    },
    "WithdrawSubmissionForm": {
        "reason": (
            "仅在审核人尚未作出结论时可撤回；旧版本仍会保留。",
            "You can withdraw only before a reviewer decides. The previous version remains in history.",
        ),
    },
}


def _localize_form(form: forms.Form) -> None:
    if not _is_english():
        return
    labels = {**FORM_TEXT, **CLASS_FORM_TEXT.get(form.__class__.__name__, {})}
    help_texts = {**FORM_HELP, **CLASS_FORM_HELP.get(form.__class__.__name__, {})}
    for name, field in form.fields.items():
        if name in labels:
            field.label = labels[name][1]
        if name in help_texts:
            field.help_text = help_texts[name][1]

TASK_CREATE_TOKEN_SALT = "growth-os.dashboard.task-create.v1"


class TaskCreateForm(forms.Form):
    task_id = forms.UUIDField(widget=forms.HiddenInput)
    command_id = forms.UUIDField(widget=forms.HiddenInput)
    task_token = forms.CharField(widget=forms.HiddenInput)
    product_profile_version = forms.ModelChoiceField(
        queryset=ProductProfileVersion.objects.none(),
        label="产品配置版本",
        empty_label="请选择已封存的产品配置",
    )
    contract_version = forms.ModelChoiceField(
        queryset=TaskContractVersion.objects.none(),
        label="任务合同版本",
        empty_label="请选择与产品配置完全对应的任务合同",
    )
    title = forms.CharField(label="任务名称", max_length=240)
    description = forms.CharField(
        label="为什么做 / 任务说明",
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="请用自然语言说明背景和目标；具体 DoR/DoD 来自所选合同。",
    )

    def __init__(self, *args, profiles, **kwargs):
        profiles = profiles.select_related("product")
        initial = kwargs.setdefault("initial", {})
        if not args and not kwargs.get("data"):
            generated_id = uuid7()
            command_id = uuid.uuid4()
            initial.setdefault("task_id", generated_id)
            initial.setdefault("command_id", command_id)
            initial.setdefault(
                "task_token",
                signing.dumps(
                    {"task_id": str(generated_id), "command_id": str(command_id)},
                    salt=TASK_CREATE_TOKEN_SALT,
                    compress=True,
                ),
            )
        super().__init__(*args, **kwargs)
        self.fields["product_profile_version"].queryset = profiles
        latest_contract_ids = [
            TaskContractVersion.objects.filter(product_profile_version_id=profile_id)
            .order_by("-version_number", "-created_at", "-id")
            .values_list("pk", flat=True)
            .first()
            for profile_id in profiles.values_list("pk", flat=True)
        ]
        self.fields["contract_version"].queryset = TaskContractVersion.objects.filter(
            pk__in=[contract_id for contract_id in latest_contract_ids if contract_id is not None],
        ).select_related("product_profile_version", "product_profile_version__product").order_by(
            "product_profile_version__product__name",
            "product_profile_version__version_number",
            "version_number",
        )
        if _is_english():
            self.fields["product_profile_version"].empty_label = "Select a sealed product profile"
            self.fields["contract_version"].empty_label = "Select the matching task contract"
        _localize_form(self)

    def clean_task_id(self):
        task_id = self.cleaned_data["task_id"]
        if task_id.version != 7 or task_id.variant != uuid.RFC_4122:
            raise forms.ValidationError("任务 ID 必须是系统生成的 UUIDv7。")
        return task_id

    def clean(self):
        cleaned = super().clean()
        task_id = cleaned.get("task_id")
        command_id = cleaned.get("command_id")
        token = cleaned.get("task_token")
        if task_id and command_id and token:
            try:
                signed = signing.loads(token, salt=TASK_CREATE_TOKEN_SALT)
            except signing.BadSignature:
                self.add_error("task_token", "任务创建令牌无效，请刷新页面后重试。")
            else:
                if (
                    not isinstance(signed, dict)
                    or signed.get("task_id") != str(task_id)
                    or signed.get("command_id") != str(command_id)
                ):
                    self.add_error("task_id", "任务 ID 或命令 ID 与服务器创建令牌不匹配。")
        profile = cleaned.get("product_profile_version")
        contract = cleaned.get("contract_version")
        if profile and not profile.is_sealed:
            self.add_error("product_profile_version", "只能使用已封存的产品配置版本。")
        if profile and contract and contract.product_profile_version_id != profile.pk:
            self.add_error("contract_version", "任务合同必须精确属于所选产品配置版本。")
        return cleaned


def criterion_label(criterion: dict, fallback: str) -> str:
    return (
        criterion.get("label")
        or criterion.get("description")
        or criterion.get("key", fallback).replace("_", " ").capitalize()
    )


class CommandForm(forms.Form):
    command_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_state_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, state_version: int, **kwargs):
        initial = kwargs.setdefault("initial", {})
        initial.setdefault("command_id", uuid.uuid4())
        initial.setdefault("expected_state_version", state_version)
        super().__init__(*args, **kwargs)
        _localize_form(self)


class CriteriaCommandForm(CommandForm):
    field_prefix = "criterion__"

    def __init__(self, *args, criteria: list[dict], state_version: int, **kwargs):
        self.criteria = criteria
        super().__init__(*args, state_version=state_version, **kwargs)
        for criterion in criteria:
            key = criterion["key"]
            required = criterion.get("required", True)
            self.fields[f"{self.field_prefix}{key}"] = forms.ChoiceField(
                label=criterion_label(criterion, "Unnamed criterion"),
                choices=CHECK_CHOICES_EN if _is_english() else CHECK_CHOICES,
                required=True,
                help_text=(
                    ("Required" if required else "Record an accurate result for optional items too")
                    if _is_english()
                    else ("必填" if required else "可选项也需要如实记录结果")
                ),
            )

    def result_rows(self, *, evidence: dict | None = None) -> list[dict]:
        evidence = evidence or {}
        return [
            {
                "criterion_key": criterion["key"],
                "result": self.cleaned_data[f"{self.field_prefix}{criterion['key']}"],
                "evidence": evidence,
            }
            for criterion in self.criteria
        ]


class DoRForm(CriteriaCommandForm):
    pass


class AssignmentForm(CommandForm):
    assignee = forms.ModelChoiceField(
        queryset=Principal.objects.none(),
        label="分配给哪位执行负责人",
        empty_label="请选择一位可执行人员",
    )

    def __init__(self, *args, operators, state_version: int, **kwargs):
        super().__init__(*args, state_version=state_version, **kwargs)
        self.fields["assignee"].queryset = operators
        if _is_english():
            self.fields["assignee"].empty_label = "Select an operator"


class StartWorkForm(CommandForm):
    pass


class ResumeDraftForm(CommandForm):
    pass


class CancelTaskForm(CommandForm):
    reason = forms.CharField(
        label="取消原因",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="任务不会被删除；系统会保留审计记录，并从 Today 主列表隐藏。",
    )
    confirm = forms.BooleanField(label="确认取消这份草稿")


class WithdrawSubmissionForm(CommandForm):
    reason = forms.CharField(
        label="撤回原因",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="仅在审核人尚未作出结论时可撤回；旧版本仍会保留。",
    )
    confirm = forms.BooleanField(label="确认撤回并重新修改")


class DeliveryDoDForm(CriteriaCommandForm):
    external_url = forms.URLField(
        label="本次交付链接",
        max_length=1024,
        help_text="必填。请填写可供审核和发布人员打开的内容链接。链接变化时需提交新版本。",
    )
    submission_note = forms.CharField(
        label="交付说明",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
