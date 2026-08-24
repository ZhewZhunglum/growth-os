from django.contrib import admin

from .models import (
    Issue,
    IssueDecisionLink,
    IssueEvent,
    IssueSourceLink,
    Meeting,
    MeetingDecision,
    MeetingParticipant,
    PolicyActivation,
    PolicyActivationEvent,
    PolicyRollbackEvent,
    RuleApprovalDecision,
    RuleProposalSourceLink,
    RuleProposalVersion,
    RuleValidationRun,
)


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ("issue_key", "issue_type", "severity", "current_state", "state_version")
    readonly_fields = ("current_state", "state_version", "created_at")
    search_fields = ("issue_key", "title")

    def has_delete_permission(self, request, obj=None):
        return False


class AppendOnlyAdmin(admin.ModelAdmin):
    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in (
    IssueEvent,
    IssueSourceLink,
    Meeting,
    MeetingParticipant,
    MeetingDecision,
    IssueDecisionLink,
    RuleProposalVersion,
    RuleProposalSourceLink,
    RuleValidationRun,
    RuleApprovalDecision,
    PolicyActivation,
    PolicyActivationEvent,
    PolicyRollbackEvent,
):
    admin.site.register(model, AppendOnlyAdmin)
