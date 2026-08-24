from __future__ import annotations

from datetime import timedelta

from django import forms
from django.utils import timezone
from django.utils.translation import get_language

from accounts.models import Principal
from governance.models import (
    Issue,
    Meeting,
    MeetingDecision,
    RuleApprovalDecision,
    RuleProposalSourceLink,
    RuleProposalVersion,
    RuleValidationRun,
)
from insights.models import LearningVersion
from releasegate.models import PolicyDefinition, PolicyVersion


ENUM_TEXT = {
    "OPERATIONAL": ("运营问题", "Operational"),
    "BLOCKER": ("阻塞", "Blocker"),
    "SAFETY_EVENT": ("安全事件", "Safety event"),
    "RULE_CONFLICT": ("规则冲突", "Rule conflict"),
    "LOW": ("低", "Low"),
    "MEDIUM": ("中", "Medium"),
    "HIGH": ("高", "High"),
    "CRITICAL": ("严重", "Critical"),
    "OPEN": ("待处理", "Open"),
    "TRIAGED": ("已初步判断", "Triaged"),
    "IN_PROGRESS": ("处理中", "In progress"),
    "RESOLVED": ("已解决", "Resolved"),
    "CLOSED": ("已关闭", "Closed"),
    "ESCALATED_TO_MEETING": ("升级到会议", "Escalate to meeting"),
    "OPERATIONAL_REVIEW": ("运营复盘", "Operational review"),
    "RULE_GOVERNANCE": ("规则治理", "Rule governance"),
    "SAFETY_INCIDENT": ("安全事件", "Safety incident"),
    "PRIMARY": ("主要问题", "Primary issue"),
    "RELATED": ("相关问题", "Related issue"),
    "RULE_PROPOSAL": ("规则提案", "Rule proposal"),
    "OPERATIONAL_FIX": ("运营修正", "Operational fix"),
    "NO_ACTION": ("无需行动", "No action"),
    "MEETING_DECISION": ("会议结论", "Meeting decision"),
    "LEARNING": ("学习结论", "Learning"),
    "ISSUE": ("问题", "Issue"),
    "OFFICIAL_POLICY": ("官方政策", "Official policy"),
    "MANUAL": ("人工提出", "Manual"),
    "TIGHTEN": ("收紧", "Tighten"),
    "RELAX": ("放松", "Relax"),
    "CLARIFY": ("澄清", "Clarify"),
    "ADD": ("新增", "Add"),
    "RETIRE": ("停用", "Retire"),
    "HISTORICAL_REPLAY": ("历史回放", "Historical replay"),
    "SHADOW": ("影子验证", "Shadow"),
    "CANARY": ("小范围试运行", "Canary"),
    "PASSED": ("通过", "Passed"),
    "FAILED": ("失败", "Failed"),
    "PARTIAL": ("部分通过", "Partial"),
    "ERROR": ("错误", "Error"),
    "APPROVED": ("批准", "Approved"),
    "REJECTED": ("拒绝", "Rejected"),
    "CHANGES_REQUESTED": ("需要修改", "Changes requested"),
}


class GovernanceLocalizedFormMixin:
    localized_labels: dict[str, tuple[str, str]] = {}
    localized_help: dict[str, tuple[str, str]] = {}

    def __init__(self, *args, **kwargs):
        self.is_english = str(get_language() or "zh-hans").lower().startswith("en")
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name in self.localized_labels:
                field.label = self.tr(*self.localized_labels[name])
            if name in self.localized_help:
                field.help_text = self.tr(*self.localized_help[name])
            if isinstance(field, forms.ChoiceField) and not isinstance(field, forms.ModelChoiceField):
                field.choices = [
                    (value, self.tr(*ENUM_TEXT[value]) if value in ENUM_TEXT else label)
                    for value, label in field.choices
                ]

    def tr(self, chinese: str, english: str) -> str:
        return english if self.is_english else chinese


class DateTimeLocalInput(forms.DateTimeInput):
    input_type = "datetime-local"


class PolicyDefinitionForm(GovernanceLocalizedFormMixin, forms.ModelForm):
    localized_labels = {
        "policy_code": ("规则编号", "Rule code"),
        "name": ("规则名称", "Rule name"),
        "description": ("规则说明", "Rule description"),
        "is_mandatory": ("发布时必须检查", "Required during release checks"),
    }
    localized_help = {
        "policy_code": (
            "稳定编号，例如 CONTENT_CLAIMS_US；创建后本页不提供修改入口。",
            "Stable code, for example CONTENT_CLAIMS_US. This page does not provide an edit path after creation.",
        ),
        "is_mandatory": (
            "选中后，这个定义的当前有效版本会进入发布检查。",
            "When selected, the current effective version is included in release checks.",
        ),
    }
    class Meta:
        model = PolicyDefinition
        fields = ("policy_code", "name", "description", "is_mandatory")
        labels = {
            "policy_code": "规则编号",
            "name": "规则名称",
            "description": "规则说明",
            "is_mandatory": "发布时必须检查",
        }
        help_texts = {
            "policy_code": "稳定编号，例如 CONTENT_CLAIMS_US；创建后本页不提供修改入口。",
            "is_mandatory": "选中后，这个定义的当前有效版本会进入发布检查。",
        }
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def validate_unique(self):
        # The controlled service owns duplicate-submit idempotency and returns
        # the existing definition only when the complete payload is identical.
        return None


class PolicyVersionForm(GovernanceLocalizedFormMixin, forms.Form):
    localized_labels = {
        "policy_definition": ("所属规则", "Rule definition"),
        "rules": ("规则清单（JSON）", "Rule list (JSON)"),
        "effective_from": ("生效时间", "Effective from"),
        "effective_until": ("失效时间（可空）", "Effective until (optional)"),
    }
    policy_definition = forms.ModelChoiceField(
        label="所属规则",
        queryset=PolicyDefinition.objects.none(),
    )
    rules = forms.JSONField(
        label="规则清单（JSON）",
        widget=forms.Textarea(attrs={"rows": 8}),
        help_text='格式：[ {"rule_code": "no_disease_claim", "required": true} ]',
    )
    effective_from = forms.DateTimeField(
        label="生效时间",
        widget=DateTimeLocalInput(format="%Y-%m-%dT%H:%M"),
    )
    effective_until = forms.DateTimeField(
        label="失效时间（可空）",
        required=False,
        widget=DateTimeLocalInput(format="%Y-%m-%dT%H:%M"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["policy_definition"].queryset = PolicyDefinition.objects.filter(
            status=PolicyDefinition.Status.ACTIVE
        ).order_by("policy_code")
        self.fields["effective_from"].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["effective_until"].input_formats = ["%Y-%m-%dT%H:%M"]
        if not self.is_bound:
            self.initial.setdefault("effective_from", timezone.localtime().replace(second=0, microsecond=0))
            self.initial.setdefault(
                "rules",
                [{"rule_code": "replace_with_rule_code", "required": True}],
            )

    def clean_rules(self):
        rules = self.cleaned_data["rules"]
        if not isinstance(rules, list) or not rules:
            raise forms.ValidationError(self.tr("至少需要一条规则。", "At least one rule is required."))
        seen = set()
        normalized = []
        for position, rule in enumerate(rules, start=1):
            if not isinstance(rule, dict):
                raise forms.ValidationError(self.tr(f"第 {position} 条规则必须是 JSON 对象。", f"Rule {position} must be a JSON object."))
            code = str(rule.get("rule_code", rule.get("code", ""))).strip()
            if not code:
                raise forms.ValidationError(self.tr(f"第 {position} 条规则缺少 rule_code。", f"Rule {position} is missing rule_code."))
            if code in seen:
                raise forms.ValidationError(self.tr(f"规则编号重复：{code}。", f"Duplicate rule code: {code}."))
            required = rule.get("required", True)
            if not isinstance(required, bool):
                raise forms.ValidationError(self.tr(f"{code} 的 required 只能是 true 或 false。", f"required for {code} must be true or false."))
            seen.add(code)
            normalized.append({"rule_code": code, "required": required})
        return sorted(normalized, key=lambda item: item["rule_code"])

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("effective_from")
        end = cleaned.get("effective_until")
        if start and end and end <= start:
            self.add_error("effective_until", self.tr("失效时间必须晚于生效时间。", "The end time must be later than the start time."))
        return cleaned


class IssueForm(GovernanceLocalizedFormMixin, forms.ModelForm):
    localized_labels = {
        "issue_key": ("问题编号", "Issue code"),
        "issue_type": ("问题类型", "Issue type"),
        "severity": ("严重程度", "Severity"),
        "title": ("一句话标题", "Short title"),
        "description": ("具体情况", "Details"),
    }
    localized_help = {
        "issue_key": (
            "例如：daily-20260821-pinterest-login；提交后不能改写。",
            "For example: daily-20260821-pinterest-login. It cannot be rewritten after submission.",
        )
    }
    class Meta:
        model = Issue
        fields = ("issue_key", "issue_type", "severity", "title", "description")
        labels = {
            "issue_key": "问题编号",
            "issue_type": "问题类型",
            "severity": "严重程度",
            "title": "一句话标题",
            "description": "具体情况",
        }
        help_texts = {"issue_key": "例如：daily-20260821-pinterest-login；提交后不能改写。"}


class IssueTransitionForm(GovernanceLocalizedFormMixin, forms.Form):
    localized_labels = {
        "to_state": ("下一步状态", "Next state"),
        "reason": ("处理说明", "Handling notes"),
    }
    to_state = forms.ChoiceField(label="下一步状态", choices=Issue.State.choices)
    reason = forms.CharField(label="处理说明", widget=forms.Textarea(attrs={"rows": 3}))


class MeetingForm(GovernanceLocalizedFormMixin, forms.ModelForm):
    localized_labels = {
        "participants": ("参会人员", "Participants"),
        "meeting_key": ("会议编号", "Meeting code"),
        "meeting_type": ("会议类型", "Meeting type"),
        "title": ("会议主题", "Meeting topic"),
        "summary": ("会议摘要", "Meeting summary"),
        "occurred_at": ("会议时间", "Meeting time"),
    }
    localized_help = {"participants": ("可多选；创建人会自动加入。", "Select any number; the creator is added automatically.")}
    participants = forms.ModelMultipleChoiceField(
        label="参会人员",
        queryset=Principal.objects.none(),
        required=False,
        help_text="可多选；创建人会自动加入。",
    )

    class Meta:
        model = Meeting
        fields = ("meeting_key", "meeting_type", "title", "summary", "occurred_at")
        labels = {
            "meeting_key": "会议编号",
            "meeting_type": "会议类型",
            "title": "会议主题",
            "summary": "会议摘要",
            "occurred_at": "会议时间",
        }
        widgets = {"occurred_at": DateTimeLocalInput(format="%Y-%m-%dT%H:%M")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["participants"].queryset = Principal.objects.filter(
            principal_type=Principal.PrincipalType.HUMAN_USER,
            principal_status=Principal.PrincipalStatus.ACTIVE,
            is_active=True,
        ).order_by("username")
        self.fields["occurred_at"].input_formats = ["%Y-%m-%dT%H:%M"]


class MeetingDecisionForm(GovernanceLocalizedFormMixin, forms.ModelForm):
    localized_labels = {
        "issue": ("关联问题（可选）", "Related issue (optional)"),
        "linkage_role": ("关联方式", "Relationship"),
        "decision_key": ("结论编号", "Decision code"),
        "decision_type": ("结论类型", "Decision type"),
        "decision": ("结构化结论", "Structured decision"),
        "impact_scope": ("影响范围（JSON）", "Impact scope (JSON)"),
        "owner_principal": ("负责人", "Owner"),
        "due_at": ("截止时间（可选）", "Due date (optional)"),
    }
    issue = forms.ModelChoiceField(
        label="关联问题（可选）",
        queryset=Issue.objects.none(),
        required=False,
    )
    linkage_role = forms.ChoiceField(
        label="关联方式",
        choices=(("PRIMARY", "主要问题"), ("RELATED", "相关问题")),
        required=False,
    )

    class Meta:
        model = MeetingDecision
        fields = ("decision_key", "decision_type", "decision", "impact_scope", "owner_principal", "due_at")
        labels = {
            "decision_key": "结论编号",
            "decision_type": "结论类型",
            "decision": "结构化结论",
            "impact_scope": "影响范围（JSON）",
            "owner_principal": "负责人",
            "due_at": "截止时间（可选）",
        }
        widgets = {
            "decision": forms.Textarea(attrs={"rows": 4}),
            "impact_scope": forms.Textarea(attrs={"rows": 3}),
            "due_at": DateTimeLocalInput(format="%Y-%m-%dT%H:%M"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["issue"].queryset = Issue.objects.exclude(current_state=Issue.State.CLOSED).order_by("-created_at")
        self.fields["owner_principal"].queryset = Principal.objects.filter(
            principal_type=Principal.PrincipalType.HUMAN_USER,
            principal_status=Principal.PrincipalStatus.ACTIVE,
            is_active=True,
        ).order_by("username")
        self.fields["due_at"].input_formats = ["%Y-%m-%dT%H:%M"]


class RuleProposalForm(GovernanceLocalizedFormMixin, forms.ModelForm):
    localized_labels = {
        "source_kind": ("提案来源", "Proposal source"),
        "meeting_decision": ("会议结论", "Meeting decision"),
        "learning_version": ("学习结论", "Learning"),
        "issue": ("问题", "Issue"),
        "official_policy_version": ("官方政策版本", "Official policy version"),
        "proposal_key": ("提案编号", "Proposal code"),
        "target_policy_definition": ("要修改的规则", "Rule to change"),
        "candidate_policy_version": ("候选规则版本", "Candidate rule version"),
        "change_effect": ("修改方向", "Change direction"),
        "risk_level": ("风险等级", "Risk level"),
        "affected_scope": ("影响范围（JSON）", "Affected scope (JSON)"),
        "rationale": ("为什么要改", "Reason for change"),
    }
    source_kind = forms.ChoiceField(label="提案来源", choices=RuleProposalSourceLink.SourceKind.choices)
    meeting_decision = forms.ModelChoiceField(label="会议结论", queryset=MeetingDecision.objects.none(), required=False)
    learning_version = forms.ModelChoiceField(label="学习结论", queryset=LearningVersion.objects.none(), required=False)
    issue = forms.ModelChoiceField(label="问题", queryset=Issue.objects.none(), required=False)
    official_policy_version = forms.ModelChoiceField(label="官方政策版本", queryset=PolicyVersion.objects.none(), required=False)

    class Meta:
        model = RuleProposalVersion
        fields = (
            "proposal_key",
            "target_policy_definition",
            "candidate_policy_version",
            "change_effect",
            "risk_level",
            "affected_scope",
            "rationale",
        )
        labels = {
            "proposal_key": "提案编号",
            "target_policy_definition": "要修改的规则",
            "candidate_policy_version": "候选规则版本",
            "change_effect": "修改方向",
            "risk_level": "风险等级",
            "affected_scope": "影响范围（JSON）",
            "rationale": "为什么要改",
        }
        widgets = {
            "affected_scope": forms.Textarea(attrs={"rows": 3}),
            "rationale": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_policy_definition"].queryset = PolicyDefinition.objects.order_by("policy_code")
        self.fields["candidate_policy_version"].queryset = PolicyVersion.objects.select_related("policy_definition").order_by(
            "policy_definition__policy_code", "-version_number"
        )
        self.fields["meeting_decision"].queryset = MeetingDecision.objects.select_related("meeting").order_by("-created_at")
        self.fields["learning_version"].queryset = LearningVersion.objects.order_by("-created_at")
        self.fields["issue"].queryset = Issue.objects.order_by("-created_at")
        self.fields["official_policy_version"].queryset = PolicyVersion.objects.select_related("policy_definition").order_by(
            "policy_definition__policy_code", "-version_number"
        )

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("source_kind")
        typed = {
            RuleProposalSourceLink.SourceKind.MEETING_DECISION: "meeting_decision",
            RuleProposalSourceLink.SourceKind.LEARNING: "learning_version",
            RuleProposalSourceLink.SourceKind.ISSUE: "issue",
            RuleProposalSourceLink.SourceKind.OFFICIAL_POLICY: "official_policy_version",
        }
        required_name = typed.get(kind)
        if required_name and not cleaned.get(required_name):
            self.add_error(required_name, self.tr("这个来源类型必须选择一条确切记录。", "Select one exact record for this source type."))
        for source_kind, field_name in typed.items():
            if source_kind != kind and cleaned.get(field_name):
                self.add_error(field_name, self.tr("只能填写与来源类型一致的一项。", "Fill only the field that matches the selected source type."))
        definition = cleaned.get("target_policy_definition")
        candidate = cleaned.get("candidate_policy_version")
        if definition and candidate and candidate.policy_definition_id != definition.pk:
            self.add_error("candidate_policy_version", self.tr("候选版本必须属于所选规则。", "The candidate version must belong to the selected rule."))
        return cleaned


class RuleValidationForm(GovernanceLocalizedFormMixin, forms.Form):
    localized_labels = {
        "validation_type": ("验证阶段", "Validation stage"),
        "result": ("结果", "Result"),
        "data_window_start": ("数据窗口开始", "Data window start"),
        "data_window_end": ("数据窗口结束", "Data window end"),
        "parameters": ("离线验证参数（JSON）", "Offline validation parameters (JSON)"),
        "false_positive_count": ("误报数", "False positives"),
        "false_negative_count": ("漏报数", "False negatives"),
        "risk_events": ("风险事件（JSON 数组）", "Risk events (JSON array)"),
    }
    validation_type = forms.ChoiceField(label="验证阶段", choices=RuleValidationRun.ValidationType.choices)
    result = forms.ChoiceField(label="结果", choices=RuleValidationRun.Result.choices)
    data_window_start = forms.DateTimeField(label="数据窗口开始", widget=DateTimeLocalInput(format="%Y-%m-%dT%H:%M"))
    data_window_end = forms.DateTimeField(label="数据窗口结束", widget=DateTimeLocalInput(format="%Y-%m-%dT%H:%M"))
    parameters = forms.JSONField(label="离线验证参数（JSON）", required=False, initial=dict, widget=forms.Textarea(attrs={"rows": 3}))
    false_positive_count = forms.IntegerField(label="误报数", min_value=0, initial=0)
    false_negative_count = forms.IntegerField(label="漏报数", min_value=0, initial=0)
    risk_events = forms.JSONField(label="风险事件（JSON 数组）", required=False, initial=list, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("data_window_start", "data_window_end"):
            self.fields[name].input_formats = ["%Y-%m-%dT%H:%M"]
        if not self.is_bound:
            now = timezone.now()
            self.initial.update(
                data_window_start=(now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M"),
                data_window_end=(now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
            )

    def clean_risk_events(self):
        value = self.cleaned_data.get("risk_events") or []
        if not isinstance(value, list):
            raise forms.ValidationError(self.tr("风险事件必须是 JSON 数组。", "Risk events must be a JSON array."))
        return value

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("data_window_start")
        end = cleaned.get("data_window_end")
        if start and end and end <= start:
            self.add_error("data_window_end", self.tr("数据窗口结束时间必须晚于开始时间。", "The data-window end must be later than its start."))
        return cleaned


class RuleApprovalForm(GovernanceLocalizedFormMixin, forms.Form):
    localized_labels = {
        "decision": ("人工决定", "Human decision"),
        "rationale": ("决定理由", "Decision rationale"),
    }
    decision = forms.ChoiceField(label="人工决定", choices=RuleApprovalDecision.Decision.choices)
    rationale = forms.CharField(label="决定理由", widget=forms.Textarea(attrs={"rows": 3}))


class PolicyActivationForm(GovernanceLocalizedFormMixin, forms.Form):
    localized_labels = {
        "activation_scope": ("启用范围（JSON）", "Activation scope (JSON)"),
        "effective_from": ("生效时间", "Effective from"),
    }
    activation_scope = forms.JSONField(label="启用范围（JSON）", initial=dict, widget=forms.Textarea(attrs={"rows": 3}))
    effective_from = forms.DateTimeField(label="生效时间", widget=DateTimeLocalInput(format="%Y-%m-%dT%H:%M"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["effective_from"].input_formats = ["%Y-%m-%dT%H:%M"]
        if not self.is_bound:
            self.initial["effective_from"] = timezone.now().strftime("%Y-%m-%dT%H:%M")


class PolicyRollbackForm(GovernanceLocalizedFormMixin, forms.Form):
    localized_labels = {
        "rollback_to_policy_version": ("回滚到", "Roll back to"),
        "reason": ("回滚原因", "Rollback reason"),
    }
    rollback_to_policy_version = forms.ModelChoiceField(label="回滚到", queryset=PolicyVersion.objects.none())
    reason = forms.CharField(label="回滚原因", widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, activation=None, **kwargs):
        super().__init__(*args, **kwargs)
        if activation is not None:
            self.fields["rollback_to_policy_version"].queryset = PolicyVersion.objects.filter(
                policy_definition=activation.policy_version.policy_definition,
                version_number__lt=activation.policy_version.version_number,
            ).order_by("-version_number")
