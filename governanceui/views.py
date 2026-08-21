from __future__ import annotations

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.models import PermissionGrant
from governance.models import Issue, Meeting, PolicyActivation, RuleProposalVersion
from governance.services import activate_policy, rollback_policy, transition_issue
from releasegate.models import PolicyDefinition
from governanceui.forms import (
    IssueForm,
    IssueTransitionForm,
    MeetingDecisionForm,
    MeetingForm,
    PolicyActivationForm,
    PolicyDefinitionForm,
    PolicyRollbackForm,
    PolicyVersionForm,
    RuleApprovalForm,
    RuleProposalForm,
    RuleValidationForm,
)
from governanceui.services import (
    append_policy_version,
    create_issue,
    create_meeting,
    create_meeting_decision,
    create_policy_definition,
    create_rule_proposal,
    record_offline_validation,
    record_rule_approval,
    require_global_grant,
    require_governance_view,
    resolve_rollback_grant,
)


def _form_error(form, error) -> None:
    if isinstance(error, ValidationError) and hasattr(error, "message_dict"):
        for field, values in error.message_dict.items():
            target = field if field in form.fields else None
            for value in values:
                form.add_error(target, value)
    else:
        form.add_error(None, str(error))


@login_required
def home(request: HttpRequest) -> HttpResponse:
    require_governance_view(request.user)
    issues = Issue.objects.order_by("-created_at")[:20]
    meetings = Meeting.objects.annotate(decision_count=Count("decisions")).order_by("-occurred_at")[:12]
    proposals = RuleProposalVersion.objects.select_related(
        "target_policy_definition", "candidate_policy_version"
    ).prefetch_related("validation_runs", "approval_decisions", "source_links").order_by("-created_at")[:20]
    policy_definitions = PolicyDefinition.objects.prefetch_related("versions").order_by("policy_code")
    return render(
        request,
        "governanceui/home.html",
        {
            "issues": issues,
            "meetings": meetings,
            "proposals": proposals,
            "open_issue_count": Issue.objects.exclude(current_state=Issue.State.CLOSED).count(),
            "active_policy_count": PolicyActivation.objects.count(),
            "policy_definitions": policy_definitions,
        },
    )


@login_required
def policy_definition_create(request: HttpRequest) -> HttpResponse:
    require_governance_view(request.user)
    form = PolicyDefinitionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            definition = create_policy_definition(actor=request.user, **form.cleaned_data)
        except (PermissionDenied, ValidationError) as error:
            _form_error(form, error)
        else:
            messages.success(request, "规则定义已保存；它本身还没有候选版本，也不会自动生效。")
            return redirect(f"{reverse('governanceui:policy-version-create')}?definition={definition.pk}")
    return render(
        request,
        "governanceui/policy_definition_form.html",
        {"form": form},
        status=400 if request.method == "POST" else 200,
    )


@login_required
def policy_version_create(request: HttpRequest) -> HttpResponse:
    require_governance_view(request.user)
    initial = {}
    if request.method != "POST" and request.GET.get("definition"):
        initial["policy_definition"] = request.GET["definition"]
    form = PolicyVersionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            version = append_policy_version(actor=request.user, **form.cleaned_data)
        except (PermissionDenied, ValidationError) as error:
            _form_error(form, error)
        else:
            messages.success(
                request,
                f"候选规则 v{version.version_number} 已封存；下一步可用它建立规则提案。",
            )
            return redirect("governanceui:proposal-create")
    return render(
        request,
        "governanceui/policy_version_form.html",
        {"form": form},
        status=400 if request.method == "POST" else 200,
    )


@login_required
def issue_create(request: HttpRequest) -> HttpResponse:
    require_governance_view(request.user)
    form = IssueForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            issue = create_issue(actor=request.user, **form.cleaned_data)
        except (PermissionDenied, ValidationError) as error:
            _form_error(form, error)
        else:
            messages.success(request, "问题已记录；它不会直接改规则。")
            return redirect("governanceui:issue-detail", issue_id=issue.pk)
    return render(request, "governanceui/issue_form.html", {"form": form}, status=400 if request.method == "POST" else 200)


def _issue_context(issue, *, transition_form=None):
    return {
        "issue": issue,
        "events": issue.events.select_related("actor_principal", "permission_grant").order_by("event_sequence"),
        "decision_links": issue.decision_links.select_related("meeting_decision__meeting").order_by("created_at"),
        "transition_form": transition_form or IssueTransitionForm(),
    }


@login_required
def issue_detail(request: HttpRequest, issue_id) -> HttpResponse:
    require_governance_view(request.user)
    issue = get_object_or_404(Issue, pk=issue_id)
    return render(request, "governanceui/issue_detail.html", _issue_context(issue))


@login_required
@require_POST
def issue_transition(request: HttpRequest, issue_id) -> HttpResponse:
    issue = get_object_or_404(Issue, pk=issue_id)
    form = IssueTransitionForm(request.POST)
    if form.is_valid():
        try:
            grant = require_global_grant(request.user, PermissionGrant.Action.EDIT)
            transition_issue(
                issue=issue,
                to_state=form.cleaned_data["to_state"],
                reason=form.cleaned_data["reason"],
                command_id=uuid.uuid4(),
                expected_state_version=issue.state_version,
                actor_principal=request.user,
                acting_role=request.user.role,
                permission_grant=grant,
            )
        except (PermissionDenied, ValidationError) as error:
            _form_error(form, error)
        else:
            messages.success(request, "问题状态已追加一条不可改写的处理记录。")
            return redirect("governanceui:issue-detail", issue_id=issue.pk)
    return render(request, "governanceui/issue_detail.html", _issue_context(issue, transition_form=form), status=400)


@login_required
def meeting_create(request: HttpRequest) -> HttpResponse:
    require_governance_view(request.user)
    form = MeetingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        participants = data.pop("participants")
        try:
            meeting = create_meeting(actor=request.user, participants=participants, **data)
        except (PermissionDenied, ValidationError) as error:
            _form_error(form, error)
        else:
            messages.success(request, "会议记录已创建；下一步补充结构化结论。")
            return redirect("governanceui:meeting-detail", meeting_id=meeting.pk)
    return render(request, "governanceui/meeting_form.html", {"form": form}, status=400 if request.method == "POST" else 200)


def _meeting_context(meeting, *, decision_form=None):
    return {
        "meeting": meeting,
        "participants": meeting.participants.select_related("principal").order_by("created_at"),
        "decisions": meeting.decisions.select_related("owner_principal").prefetch_related("issue_links__issue").order_by("created_at"),
        "decision_form": decision_form or MeetingDecisionForm(),
    }


@login_required
def meeting_detail(request: HttpRequest, meeting_id) -> HttpResponse:
    require_governance_view(request.user)
    meeting = get_object_or_404(Meeting, pk=meeting_id)
    return render(request, "governanceui/meeting_detail.html", _meeting_context(meeting))


@login_required
@require_POST
def meeting_decision_create(request: HttpRequest, meeting_id) -> HttpResponse:
    meeting = get_object_or_404(Meeting, pk=meeting_id)
    form = MeetingDecisionForm(request.POST)
    if form.is_valid():
        data = dict(form.cleaned_data)
        issue = data.pop("issue")
        linkage_role = data.pop("linkage_role")
        try:
            create_meeting_decision(
                actor=request.user,
                meeting=meeting,
                issue=issue,
                linkage_role=linkage_role,
                **data,
            )
        except (PermissionDenied, ValidationError) as error:
            _form_error(form, error)
        else:
            messages.success(request, "结构化会议结论已保存。")
            return redirect("governanceui:meeting-detail", meeting_id=meeting.pk)
    return render(request, "governanceui/meeting_detail.html", _meeting_context(meeting, decision_form=form), status=400)


@login_required
def proposal_create(request: HttpRequest) -> HttpResponse:
    require_governance_view(request.user)
    form = RuleProposalForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        source_kind = data.pop("source_kind")
        source_fields = {
            "MEETING_DECISION": "meeting_decision",
            "LEARNING": "learning_version",
            "ISSUE": "issue",
            "OFFICIAL_POLICY": "official_policy_version",
        }
        source = data.pop(source_fields[source_kind], None) if source_kind in source_fields else None
        for field in ("meeting_decision", "learning_version", "issue", "official_policy_version"):
            data.pop(field, None)
        try:
            proposal = create_rule_proposal(actor=request.user, source_kind=source_kind, source=source, **data)
        except (PermissionDenied, ValidationError) as error:
            _form_error(form, error)
        else:
            messages.success(request, "规则提案已创建；它还没有生效。")
            return redirect("governanceui:proposal-detail", proposal_id=proposal.pk)
    return render(request, "governanceui/proposal_form.html", {"form": form}, status=400 if request.method == "POST" else 200)


def _proposal_context(proposal, **overrides):
    activation = PolicyActivation.objects.filter(rule_proposal_version=proposal).select_related(
        "policy_version__policy_definition"
    ).first()
    context = {
        "proposal": proposal,
        "sources": proposal.source_links.select_related(
            "meeting_decision", "learning_version", "issue", "official_policy_version"
        ).order_by("created_at"),
        "validation_runs": proposal.validation_runs.order_by("started_at", "created_at"),
        "approval_decisions": proposal.approval_decisions.select_related(
            "approver_principal", "permission_grant"
        ).order_by("decided_at", "id"),
        "activation": activation,
        "validation_form": RuleValidationForm(),
        "approval_form": RuleApprovalForm(),
        "activation_form": PolicyActivationForm(),
        "rollback_form": PolicyRollbackForm(activation=activation) if activation else None,
    }
    context.update(overrides)
    return context


@login_required
def proposal_detail(request: HttpRequest, proposal_id) -> HttpResponse:
    require_governance_view(request.user)
    proposal = get_object_or_404(
        RuleProposalVersion.objects.select_related("target_policy_definition", "candidate_policy_version"),
        pk=proposal_id,
    )
    return render(request, "governanceui/proposal_detail.html", _proposal_context(proposal))


@login_required
@require_POST
def proposal_validate(request: HttpRequest, proposal_id) -> HttpResponse:
    proposal = get_object_or_404(RuleProposalVersion, pk=proposal_id)
    form = RuleValidationForm(request.POST)
    if form.is_valid():
        try:
            record_offline_validation(actor=request.user, proposal=proposal, **form.cleaned_data)
        except (PermissionDenied, ValidationError) as error:
            _form_error(form, error)
        else:
            messages.success(request, "离线验证结果已记录；没有调用外部 API。")
            return redirect("governanceui:proposal-detail", proposal_id=proposal.pk)
    return render(request, "governanceui/proposal_detail.html", _proposal_context(proposal, validation_form=form), status=400)


@login_required
@require_POST
def proposal_approve(request: HttpRequest, proposal_id) -> HttpResponse:
    proposal = get_object_or_404(RuleProposalVersion, pk=proposal_id)
    form = RuleApprovalForm(request.POST)
    if form.is_valid():
        try:
            record_rule_approval(actor=request.user, proposal=proposal, **form.cleaned_data)
        except (PermissionDenied, ValidationError) as error:
            _form_error(form, error)
        else:
            messages.success(request, "人工决定已记录。通过也不代表规则已经启用。")
            return redirect("governanceui:proposal-detail", proposal_id=proposal.pk)
    return render(request, "governanceui/proposal_detail.html", _proposal_context(proposal, approval_form=form), status=400)


@login_required
@require_POST
def proposal_activate(request: HttpRequest, proposal_id) -> HttpResponse:
    proposal = get_object_or_404(RuleProposalVersion, pk=proposal_id)
    form = PolicyActivationForm(request.POST)
    if form.is_valid():
        try:
            grant = require_global_grant(request.user, PermissionGrant.Action.APPROVE)
            activate_policy(
                proposal=proposal,
                activation_scope=form.cleaned_data["activation_scope"],
                effective_from=form.cleaned_data["effective_from"],
                command_id=uuid.uuid4(),
                actor_principal=request.user,
                acting_role=request.user.role,
                permission_grant=grant,
            )
        except (PermissionDenied, ValidationError) as error:
            _form_error(form, error)
        else:
            messages.success(request, "规则已按确切候选版本受控启用。")
            return redirect("governanceui:proposal-detail", proposal_id=proposal.pk)
    return render(request, "governanceui/proposal_detail.html", _proposal_context(proposal, activation_form=form), status=400)


@login_required
@require_POST
def activation_rollback(request: HttpRequest, activation_id) -> HttpResponse:
    activation = get_object_or_404(
        PolicyActivation.objects.select_related("rule_proposal_version", "policy_version__policy_definition"),
        pk=activation_id,
    )
    form = PolicyRollbackForm(request.POST, activation=activation)
    if form.is_valid():
        try:
            grant = resolve_rollback_grant(request.user)
            rollback_policy(
                activation=activation,
                rollback_to_policy_version=form.cleaned_data["rollback_to_policy_version"],
                reason=form.cleaned_data["reason"],
                command_id=uuid.uuid4(),
                actor_principal=request.user,
                acting_role=request.user.role,
                permission_grant=grant,
            )
        except (PermissionDenied, ValidationError) as error:
            _form_error(form, error)
        else:
            messages.success(request, "已回滚到确切的历史规则版本，原激活记录继续保留。")
            return redirect("governanceui:proposal-detail", proposal_id=activation.rule_proposal_version_id)
    proposal = activation.rule_proposal_version
    return render(request, "governanceui/proposal_detail.html", _proposal_context(proposal, rollback_form=form), status=400)
