from __future__ import annotations

from datetime import timedelta

from django import forms
from django.utils import timezone

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


class DateTimeLocalInput(forms.DateTimeInput):
    input_type = "datetime-local"


class PolicyDefinitionForm(forms.ModelForm):
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


class PolicyVersionForm(forms.Form):
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
            raise forms.ValidationError("至少需要一条规则。")
        seen = set()
        normalized = []
        for position, rule in enumerate(rules, start=1):
            if not isinstance(rule, dict):
                raise forms.ValidationError(f"第 {position} 条规则必须是 JSON 对象。")
            code = str(rule.get("rule_code", rule.get("code", ""))).strip()
            if not code:
                raise forms.ValidationError(f"第 {position} 条规则缺少 rule_code。")
            if code in seen:
                raise forms.ValidationError(f"规则编号重复：{code}。")
            required = rule.get("required", True)
            if not isinstance(required, bool):
                raise forms.ValidationError(f"{code} 的 required 只能是 true 或 false。")
            seen.add(code)
            normalized.append({"rule_code": code, "required": required})
        return sorted(normalized, key=lambda item: item["rule_code"])

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("effective_from")
        end = cleaned.get("effective_until")
        if start and end and end <= start:
            self.add_error("effective_until", "失效时间必须晚于生效时间。")
        return cleaned


class IssueForm(forms.ModelForm):
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


class IssueTransitionForm(forms.Form):
    to_state = forms.ChoiceField(label="下一步状态", choices=Issue.State.choices)
    reason = forms.CharField(label="处理说明", widget=forms.Textarea(attrs={"rows": 3}))


class MeetingForm(forms.ModelForm):
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


class MeetingDecisionForm(forms.ModelForm):
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


class RuleProposalForm(forms.ModelForm):
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
            self.add_error(required_name, "这个来源类型必须选择一条确切记录。")
        for source_kind, field_name in typed.items():
            if source_kind != kind and cleaned.get(field_name):
                self.add_error(field_name, "只能填写与来源类型一致的一项。")
        definition = cleaned.get("target_policy_definition")
        candidate = cleaned.get("candidate_policy_version")
        if definition and candidate and candidate.policy_definition_id != definition.pk:
            self.add_error("candidate_policy_version", "候选版本必须属于所选规则。")
        return cleaned


class RuleValidationForm(forms.Form):
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
            raise forms.ValidationError("风险事件必须是 JSON 数组。")
        return value

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("data_window_start")
        end = cleaned.get("data_window_end")
        if start and end and end <= start:
            self.add_error("data_window_end", "数据窗口结束时间必须晚于开始时间。")
        return cleaned


class RuleApprovalForm(forms.Form):
    decision = forms.ChoiceField(label="人工决定", choices=RuleApprovalDecision.Decision.choices)
    rationale = forms.CharField(label="决定理由", widget=forms.Textarea(attrs={"rows": 3}))


class PolicyActivationForm(forms.Form):
    activation_scope = forms.JSONField(label="启用范围（JSON）", initial=dict, widget=forms.Textarea(attrs={"rows": 3}))
    effective_from = forms.DateTimeField(label="生效时间", widget=DateTimeLocalInput(format="%Y-%m-%dT%H:%M"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["effective_from"].input_formats = ["%Y-%m-%dT%H:%M"]
        if not self.is_bound:
            self.initial["effective_from"] = timezone.now().strftime("%Y-%m-%dT%H:%M")


class PolicyRollbackForm(forms.Form):
    rollback_to_policy_version = forms.ModelChoiceField(label="回滚到", queryset=PolicyVersion.objects.none())
    reason = forms.CharField(label="回滚原因", widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, activation=None, **kwargs):
        super().__init__(*args, **kwargs)
        if activation is not None:
            self.fields["rollback_to_policy_version"].queryset = PolicyVersion.objects.filter(
                policy_definition=activation.policy_version.policy_definition,
                version_number__lt=activation.policy_version.version_number,
            ).order_by("-version_number")
