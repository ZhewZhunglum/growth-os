from __future__ import annotations

import uuid

from django import forms
from django.core import signing
from django.utils.translation import get_language

from accounts.models import Principal
from contentops.models import ContentAssetVersion
from core.ids import uuid7
from products.models import ProductProfileVersion
from workflow.models import Task, TaskCheckRun, TaskContractVersion


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
    "delivery_mode": ("这次送审哪份内容", "Content to submit"),
    "content_version": ("系统内完整内容", "Complete content in Growth OS"),
    "inline_content": ("完整发布内容", "Complete publishable content"),
    "submission_note": ("交付说明（选填）", "Delivery note (optional)"),
}


PLAIN_CRITERION_LABELS = {
    "inputs_complete": (
        "开始前需要的资料、目标和账号都已准备好",
        "The information, goal, and account needed to start are ready",
    ),
    "primary_deliverable": (
        "已经完成一份可以直接送审的完整内容",
        "A complete version is ready to send for review",
    ),
    "primary-deliverable": (
        "已经完成一份可以直接送审的完整内容",
        "A complete version is ready to send for review",
    ),
    "exact_release_context": (
        "发布前重新核对账号、环境、权限和规则",
        "Recheck the account, environment, permission, and rules before publishing",
    ),
    "exact-release-context": (
        "发布前重新核对账号、环境、权限和规则",
        "Recheck the account, environment, permission, and rules before publishing",
    ),
}

FORM_HELP = {
    "description": (
        "请用自然语言说明背景和目标；具体 DoR/DoD 来自所选合同。",
        "Explain the context and goal in plain language. Readiness and delivery checks come from the selected contract.",
    ),
    "external_url": (
        "仅在选择“外部链接”时填写。链接变化时需提交新版本。",
        "Complete this only when External link is selected. A changed link requires a new submission.",
    ),
    "content_version": (
        "选择系统刚生成或你刚保存的最新完整内容版本。送审后该版本不会被改写。",
        "Select the latest complete version generated or saved here. The submitted version cannot be rewritten.",
    ),
}

CLASS_FORM_TEXT = {
    "CancelTaskForm": {
        "reason": ("取消原因（选填）", "Reason for cancellation (optional)"),
        "confirm": ("确认取消这份草稿", "I confirm that this draft should be cancelled"),
    },
    "WithdrawSubmissionForm": {
        "reason": ("撤回原因（选填）", "Reason for withdrawal (optional)"),
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
    key = criterion.get("key", "")
    if key in PLAIN_CRITERION_LABELS and not criterion.get("label") and not criterion.get("description"):
        labels = PLAIN_CRITERION_LABELS[key]
        return labels[1] if _is_english() else labels[0]
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
    expected_current_assignment_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    assignee = forms.ModelChoiceField(
        queryset=Principal.objects.none(),
        label="分配给哪位执行负责人",
        empty_label="请选择一位可执行人员",
    )

    def __init__(self, *args, operators, state_version: int, current_assignment=None, **kwargs):
        initial = kwargs.setdefault("initial", {})
        initial.setdefault(
            "expected_current_assignment_id",
            getattr(current_assignment, "pk", None),
        )
        super().__init__(*args, state_version=state_version, **kwargs)
        self.fields["assignee"].queryset = operators
        if _is_english():
            self.fields["assignee"].empty_label = "Select an operator"


class StartWorkForm(CommandForm):
    pass


class ContentGenerateForm(CommandForm):
    """Explicit offline generation command; the view never selects a live provider."""


class ContentRevisionForm(CommandForm):
    source_version = forms.ModelChoiceField(
        queryset=ContentAssetVersion.objects.none(),
        widget=forms.HiddenInput,
    )
    inline_content = forms.CharField(
        label="完整发布内容",
        min_length=1,
        max_length=50_000,
        widget=forms.Textarea(attrs={"rows": 18}),
        help_text="保存会创建不可变的新版本；旧版本仍会保留，不会被覆盖。",
    )

    def __init__(self, *args, source_versions, state_version: int, **kwargs):
        super().__init__(*args, state_version=state_version, **kwargs)
        self.fields["source_version"].queryset = source_versions
        if _is_english():
            self.fields["inline_content"].help_text = (
                "Saving creates a new immutable version. The previous version remains unchanged."
            )


class ResumeDraftForm(CommandForm):
    pass


class CancelTaskForm(CommandForm):
    reason = forms.CharField(
        label="取消原因",
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="任务不会被删除；系统会保留审计记录，并从 Today 主列表隐藏。",
    )
    confirm = forms.BooleanField(label="确认取消这份草稿")

    def __init__(self, *args, state_version: int, task_state: str = Task.State.DRAFT, **kwargs):
        super().__init__(*args, state_version=state_version, **kwargs)
        if task_state == Task.State.DRAFT:
            zh_reason = "删除草稿原因（选填）"
            zh_confirm = "我确认删除这份草稿（历史仍保留）"
            en_reason = "Reason for removing the draft (optional)"
            en_confirm = "I confirm removing this draft (history remains)"
        elif task_state == Task.State.UNDER_REVIEW:
            zh_reason = "撤回并放弃原因（选填）"
            zh_confirm = "我确认撤回送审并放弃这项任务"
            en_reason = "Reason for withdrawing and abandoning (optional)"
            en_confirm = "I confirm withdrawing the submission and abandoning this task"
        else:
            zh_reason = "放弃任务原因（选填）"
            zh_confirm = "我确认放弃这项任务（历史仍保留）"
            en_reason = "Reason for abandoning the task (optional)"
            en_confirm = "I confirm abandoning this task (history remains)"
        self.fields["reason"].label = en_reason if _is_english() else zh_reason
        self.fields["confirm"].label = en_confirm if _is_english() else zh_confirm
        self.fields["reason"].help_text = (
            "The task and any sealed submission remain in audit history; they are never physically deleted."
            if _is_english()
            else "任务和已封存的提交都会保留在审计历史中，不会被物理删除。"
        )


class WithdrawSubmissionForm(CommandForm):
    reason = forms.CharField(
        label="撤回原因",
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="仅在审核人尚未作出结论时可撤回；旧版本仍会保留。",
    )
    confirm = forms.BooleanField(label="确认撤回并重新修改")


class DeliveryDoDForm(CriteriaCommandForm):
    class DeliveryMode:
        SYSTEM_CONTENT = "SYSTEM_CONTENT"
        EXTERNAL_URL = "EXTERNAL_URL"

    delivery_mode = forms.ChoiceField(
        label="这次送审哪份内容",
        choices=(
            ("SYSTEM_CONTENT", "送审系统内的完整内容"),
            ("EXTERNAL_URL", "送审外部内容链接"),
        ),
        widget=forms.RadioSelect,
    )
    content_version = forms.ModelChoiceField(
        queryset=ContentAssetVersion.objects.none(),
        label="系统内完整内容",
        required=False,
        empty_label="请选择最新内容版本",
        help_text="选择系统刚生成或你刚保存的最新完整内容版本。送审后该版本不会被改写。",
    )
    external_url = forms.URLField(
        label="本次交付链接",
        required=False,
        max_length=1024,
        help_text="仅在选择“外部链接”时填写。链接变化时需提交新版本。",
    )
    submission_note = forms.CharField(
        label="交付说明",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(
        self,
        *args,
        content_versions=None,
        require_inline_primary: bool = False,
        task: Task | None = None,
        state_version: int,
        **kwargs,
    ):
        self.require_inline_primary = require_inline_primary
        self.task = task
        if args and args[0] is not None and "delivery_mode" not in args[0]:
            # Backward-compatible server-side inference for existing clients.
            # The current UI always posts the explicit radio choice.
            data = args[0].copy()
            if data.get("external_url"):
                data["delivery_mode"] = self.DeliveryMode.EXTERNAL_URL
            elif data.get("content_version"):
                data["delivery_mode"] = self.DeliveryMode.SYSTEM_CONTENT
            args = (data, *args[1:])
        super().__init__(*args, state_version=state_version, **kwargs)
        if require_inline_primary:
            self.fields["delivery_mode"].choices = (
                (self.DeliveryMode.SYSTEM_CONTENT, "送审系统内的完整内容"),
            )
            self.fields["external_url"].widget = forms.HiddenInput()
            self.fields["external_url"].disabled = True
        queryset = content_versions or ContentAssetVersion.objects.none()
        self.fields["content_version"].queryset = queryset
        has_system_content = queryset.exists()
        self.fields["delivery_mode"].initial = (
            self.DeliveryMode.SYSTEM_CONTENT
            if (has_system_content or require_inline_primary)
            else self.DeliveryMode.EXTERNAL_URL
        )
        if not has_system_content:
            self.fields["content_version"].disabled = True
            self.fields["content_version"].empty_label = "请先生成完整内容"
        elif not self.is_bound:
            # The UI is intentionally one-way for beginners: once a valid
            # immutable version exists, choose the newest one for them.  The
            # service still revalidates it under the Task lock on submit.
            self.fields["content_version"].initial = queryset.first()
        self.fields["submission_note"].label = (
            "Delivery note (optional)" if _is_english() else "交付说明（选填）"
        )
        self.fields["submission_note"].help_text = (
            "You may continue without a note; the system records that explicitly."
            if _is_english()
            else "不填写也可以继续；系统会明确记录为未填写。"
        )
        if _is_english():
            self.fields["delivery_mode"].choices = (
                ((self.DeliveryMode.SYSTEM_CONTENT, "Submit complete content saved in Growth OS"),)
                if require_inline_primary
                else (
                    (self.DeliveryMode.SYSTEM_CONTENT, "Submit complete content saved in Growth OS"),
                    (self.DeliveryMode.EXTERNAL_URL, "Submit an external content link"),
                )
            )
            self.fields["content_version"].empty_label = (
                "Generate complete content first" if not has_system_content else "Select the latest content version"
            )

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("delivery_mode")
        content_version = cleaned.get("content_version")
        external_url = cleaned.get("external_url")
        if self.require_inline_primary and mode != self.DeliveryMode.SYSTEM_CONTENT:
            self.add_error(
                "delivery_mode",
                "Daily Operations 发布任务必须送审系统内完整正文；外部链接只能作为参考。",
            )
        if mode == self.DeliveryMode.SYSTEM_CONTENT:
            if content_version is None:
                self.add_error("content_version", "请先生成或保存一份系统内完整内容。")
            elif self.task is not None:
                if content_version.content_asset.task_id != self.task.pk:
                    self.add_error("content_version", "所选内容不属于这项任务。")
                elif (
                    content_version.representation_kind
                    != ContentAssetVersion.RepresentationKind.INLINE_TEXT
                ):
                    self.add_error("content_version", "主要交付必须是系统内完整正文。")
                elif not content_version.inline_content.strip():
                    self.add_error("content_version", "完整正文不能为空。")
            if external_url:
                self.add_error("external_url", "送审系统内内容时不要同时填写外部链接。")
        elif mode == self.DeliveryMode.EXTERNAL_URL:
            if not external_url:
                self.add_error("external_url", "选择外部链接时必须填写网址。")
            if content_version is not None:
                self.add_error("content_version", "送审外部链接时不要同时选择系统内内容。")
        return cleaned
