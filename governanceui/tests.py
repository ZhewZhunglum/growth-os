from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from governance.models import (
    Issue,
    IssueDecisionLink,
    Meeting,
    PolicyActivation,
    RuleApprovalDecision,
    RuleProposalSourceLink,
    RuleProposalVersion,
    RuleValidationRun,
)
from insights.models import LearningVersion
from products.models import Product
from releasegate.models import PolicyDefinition, PolicyVersion, canonical_sha256


@override_settings(ROOT_URLCONF="governanceui.test_urls")
class GovernanceUIFlowTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.owner = Principal.objects.create_user(
            username="gov-ui-owner",
            password="safe-test-password-123",
            role=Principal.Role.OWNER,
        )
        self.admin = Principal.objects.create_user(
            username="gov-ui-admin",
            password="safe-test-password-123",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        self.operator = Principal.objects.create_user(
            username="gov-ui-operator",
            password="safe-test-password-123",
            role=Principal.Role.OPERATOR,
        )
        self.system = Principal.objects.create_user(
            username="gov-ui-system",
            password="safe-test-password-123",
            role=Principal.Role.OPERATOR,
            principal_type=Principal.PrincipalType.SYSTEM,
        )
        for action in (
            PermissionGrant.Action.VIEW,
            PermissionGrant.Action.EDIT,
            PermissionGrant.Action.APPROVE,
        ):
            self.grant(self.owner, action)
            self.grant(self.admin, action)
        self.grant(self.operator, PermissionGrant.Action.EDIT)
        self.grant(self.system, PermissionGrant.Action.VIEW)
        self.grant(self.system, PermissionGrant.Action.APPROVE)
        self.product = Product.objects.create(
            product_code="PUKO-GOV-UI",
            name="PUKO Governance UI",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.definition = PolicyDefinition.objects.create(
            policy_code="gov-ui-policy",
            name="Governance UI policy",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.previous_policy = PolicyVersion.objects.create(
            policy_definition=self.definition,
            version_number=1,
            rules=[{"rule_code": "safe-v1"}],
            effective_from=self.now - timedelta(days=2),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self.candidate_policy = PolicyVersion.objects.create(
            policy_definition=self.definition,
            version_number=2,
            rules=[{"rule_code": "safe-v2"}],
            effective_from=self.now,
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )

    def grant(self, principal, action):
        return PermissionGrant.objects.create(
            principal=principal,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
            action=action,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.HIGH,
            valid_from=self.now - timedelta(hours=1),
            valid_until=self.now + timedelta(days=1),
            granted_by_principal=self.owner if hasattr(self, "owner") else principal,
        )

    def test_home_requires_real_view_grant(self):
        self.client.force_login(self.operator)
        response = self.client.get(reverse("governanceui:home"))
        self.assertEqual(response.status_code, 403)
        self.grant(self.operator, PermissionGrant.Action.VIEW)
        response = self.client.get(reverse("governanceui:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "问题与规则治理")

    def test_admin_can_create_definition_idempotently_but_operator_cannot(self):
        payload = {
            "policy_code": "daily-content-safety",
            "name": "Daily content safety",
            "description": "Human-reviewed candidate rules.",
            "is_mandatory": "on",
        }
        self.client.force_login(self.admin)
        response = self.client.post(reverse("governanceui:policy-definition-create"), payload)
        self.assertEqual(response.status_code, 302, response.content.decode())
        response = self.client.post(reverse("governanceui:policy-definition-create"), payload)
        self.assertEqual(response.status_code, 302, response.content.decode())
        self.assertEqual(PolicyDefinition.objects.filter(policy_code="daily-content-safety").count(), 1)

        conflict = dict(payload, name="Conflicting replacement")
        response = self.client.post(reverse("governanceui:policy-definition-create"), conflict)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "内容不同", status_code=400)

        self.grant(self.operator, PermissionGrant.Action.VIEW)
        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("governanceui:policy-definition-create"),
            dict(payload, policy_code="operator-must-not-create"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PolicyDefinition.objects.filter(policy_code="operator-must-not-create").exists())

    def test_admin_appends_idempotent_immutable_version_with_exact_manifest(self):
        definition = PolicyDefinition.objects.create(
            policy_code="daily-version-ui",
            name="Daily version UI",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        effective_from = (self.now + timedelta(days=1)).replace(second=0, microsecond=0)
        payload = {
            "policy_definition": str(definition.pk),
            "rules": '[{"rule_code":"must_pass","required":true},{"rule_code":"observe_only","required":false}]',
            "effective_from": effective_from.strftime("%Y-%m-%dT%H:%M"),
            "effective_until": "",
        }
        self.client.force_login(self.admin)
        response = self.client.post(reverse("governanceui:policy-version-create"), payload)
        self.assertEqual(response.status_code, 302, response.content.decode())
        response = self.client.post(reverse("governanceui:policy-version-create"), payload)
        self.assertEqual(response.status_code, 302, response.content.decode())
        self.assertEqual(definition.versions.count(), 1)
        version = definition.versions.get()
        self.assertEqual(version.version_number, 1)
        self.assertEqual(
            version.rules,
            [
                {"rule_code": "must_pass", "required": True},
                {"rule_code": "observe_only", "required": False},
            ],
        )
        self.assertEqual(version.manifest_sha256, canonical_sha256(version.manifest_payload()))
        version.rules = [{"rule_code": "rewritten", "required": True}]
        with self.assertRaises(ValidationError):
            version.save()

    def test_candidate_version_rejects_ambiguous_required_value(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("governanceui:policy-version-create"),
            {
                "policy_definition": str(self.definition.pk),
                "rules": '[{"rule_code":"ambiguous","required":"false"}]',
                "effective_from": self.now.strftime("%Y-%m-%dT%H:%M"),
                "effective_until": "",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "只能是 true 或 false", status_code=400)

    def test_issue_submission_and_transition_keep_exact_grant(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("governanceui:issue-create"),
            {
                "issue_key": "gov-ui-login-blocked",
                "issue_type": Issue.IssueType.BLOCKER,
                "severity": Issue.Severity.HIGH,
                "title": "浏览器登录失效",
                "description": "Daily Task 需要人工重新登录。",
            },
        )
        self.assertEqual(response.status_code, 302)
        issue = Issue.objects.get(issue_key="gov-ui-login-blocked")
        creation_grant = issue.permission_grant
        self.assertEqual(creation_grant.principal, self.owner)
        self.assertEqual(creation_grant.action, PermissionGrant.Action.EDIT)
        self.assertEqual(creation_grant.scope_kind, PermissionGrant.ScopeKind.GLOBAL)
        response = self.client.post(
            reverse("governanceui:issue-transition", args=[issue.pk]),
            {"to_state": Issue.State.TRIAGED, "reason": "负责人已经确认，需要今天处理。"},
        )
        self.assertEqual(response.status_code, 302)
        issue.refresh_from_db()
        event = issue.events.get()
        self.assertEqual(issue.current_state, Issue.State.TRIAGED)
        self.assertEqual(event.permission_grant.action, PermissionGrant.Action.EDIT)
        self.assertEqual(event.actor_principal, self.owner)

    def test_structured_meeting_decision_can_link_exact_issue(self):
        issue = Issue.objects.create(
            issue_key="gov-ui-rule-conflict",
            issue_type=Issue.IssueType.RULE_CONFLICT,
            severity=Issue.Severity.MEDIUM,
            title="措辞规则冲突",
            description="两个规则给出了相反结论。",
            created_by_principal=self.owner,
            permission_grant=PermissionGrant.objects.get(
                principal=self.owner,
                action=PermissionGrant.Action.EDIT,
            ),
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("governanceui:meeting-create"),
            {
                "meeting_key": "gov-ui-meeting-1",
                "meeting_type": Meeting.MeetingType.RULE_GOVERNANCE,
                "title": "规则冲突复盘",
                "summary": "决定先做离线验证。",
                "occurred_at": self.now.strftime("%Y-%m-%dT%H:%M"),
                "participants": [str(self.owner.pk), str(self.admin.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        meeting = Meeting.objects.get(meeting_key="gov-ui-meeting-1")
        self.assertEqual(meeting.participants.count(), 2)
        self.assertEqual(meeting.permission_grant.principal, self.owner)
        self.assertEqual(meeting.permission_grant.action, PermissionGrant.Action.APPROVE)
        response = self.client.post(
            reverse("governanceui:meeting-decision-create", args=[meeting.pk]),
            {
                "decision_key": "decision-1",
                "decision_type": "RULE_PROPOSAL",
                "decision": "建立收紧规则提案并完成 Replay。",
                "impact_scope": '{"market":"US"}',
                "owner_principal": str(self.admin.pk),
                "due_at": "",
                "issue": str(issue.pk),
                "linkage_role": "PRIMARY",
            },
        )
        self.assertEqual(response.status_code, 302)
        link = IssueDecisionLink.objects.get(issue=issue)
        self.assertEqual(link.meeting_decision.meeting, meeting)
        self.assertEqual(link.linkage_role, "PRIMARY")
        self.assertEqual(link.permission_grant, link.meeting_decision.permission_grant)
        self.assertEqual(link.meeting_decision.permission_grant.principal, self.owner)
        self.assertEqual(link.meeting_decision.permission_grant.action, PermissionGrant.Action.APPROVE)

    def _create_learning_proposal(self, effect=RuleProposalVersion.ChangeEffect.TIGHTEN):
        learning = LearningVersion.objects.create(
            learning_key=f"gov-ui-learning-{effect.lower()}",
            version_number=1,
            product=self.product,
            title="Daily observation",
            conclusion="A repeated compliance risk was found.",
            recommended_action="Propose a bounded rule change.",
            confidence=Decimal("0.8000"),
            created_by_principal=self.owner,
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("governanceui:proposal-create"),
            {
                "proposal_key": f"gov-ui-{effect.lower()}",
                "target_policy_definition": str(self.definition.pk),
                "candidate_policy_version": str(self.candidate_policy.pk),
                "change_effect": effect,
                "risk_level": RuleProposalVersion.RiskLevel.HIGH,
                "affected_scope": '{"market":"US"}',
                "rationale": "Bounded change from an accepted learning.",
                "source_kind": RuleProposalSourceLink.SourceKind.LEARNING,
                "learning_version": str(learning.pk),
                "meeting_decision": "",
                "issue": "",
                "official_policy_version": "",
            },
        )
        self.assertEqual(response.status_code, 302, response.content.decode())
        proposal = RuleProposalVersion.objects.get(proposal_key=f"gov-ui-{effect.lower()}")
        self.assertEqual(proposal.source_links.get().learning_version, learning)
        self.assertEqual(proposal.permission_grant.principal, self.owner)
        self.assertEqual(proposal.permission_grant.action, PermissionGrant.Action.EDIT)
        self.assertEqual(proposal.source_links.get().permission_grant, proposal.permission_grant)
        return proposal

    def _validation_post(self, proposal, validation_type, result=RuleValidationRun.Result.PASSED):
        return self.client.post(
            reverse("governanceui:proposal-validate", args=[proposal.pk]),
            {
                "validation_type": validation_type,
                "result": result,
                "data_window_start": (self.now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M"),
                "data_window_end": (self.now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
                "parameters": '{"offline_fixture":"daily-v1"}',
                "false_positive_count": 0,
                "false_negative_count": 0,
                "risk_events": "[]",
            },
        )

    def test_learning_never_auto_activates_and_full_human_flow_can_rollback(self):
        proposal = self._create_learning_proposal()
        self.assertFalse(PolicyActivation.objects.filter(rule_proposal_version=proposal).exists())
        self.assertEqual(
            self._validation_post(proposal, RuleValidationRun.ValidationType.HISTORICAL_REPLAY).status_code,
            302,
        )
        self.assertEqual(
            self._validation_post(proposal, RuleValidationRun.ValidationType.HISTORICAL_REPLAY).status_code,
            302,
        )
        self.assertEqual(
            proposal.validation_runs.filter(
                validation_type=RuleValidationRun.ValidationType.HISTORICAL_REPLAY
            ).count(),
            1,
        )
        self.assertEqual(self._validation_post(proposal, RuleValidationRun.ValidationType.SHADOW).status_code, 302)
        before_approval = self._validation_post(proposal, RuleValidationRun.ValidationType.CANARY)
        self.assertEqual(before_approval.status_code, 400)
        self.assertFalse(proposal.validation_runs.filter(validation_type=RuleValidationRun.ValidationType.CANARY).exists())

        response = self.client.post(
            reverse("governanceui:proposal-approve", args=[proposal.pk]),
            {"decision": RuleApprovalDecision.Decision.APPROVED, "rationale": "Owner explicitly approves Canary."},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._validation_post(proposal, RuleValidationRun.ValidationType.CANARY).status_code, 302)
        response = self.client.post(
            reverse("governanceui:proposal-activate", args=[proposal.pk]),
            {
                "activation_scope": '{"market":"US"}',
                "effective_from": timezone.now().strftime("%Y-%m-%dT%H:%M"),
            },
        )
        self.assertEqual(response.status_code, 302, response.content.decode())
        activation = PolicyActivation.objects.get(rule_proposal_version=proposal)
        self.assertEqual(activation.activated_by_principal, self.owner)

        response = self.client.post(
            reverse("governanceui:activation-rollback", args=[activation.pk]),
            {
                "rollback_to_policy_version": str(self.previous_policy.pk),
                "reason": "Canary monitoring found unexpected risk.",
            },
        )
        self.assertEqual(response.status_code, 302, response.content.decode())
        self.assertEqual(activation.rollback_event.rollback_to_policy_version, self.previous_policy)

    def test_relax_rejects_admin_approval_and_system_activation(self):
        proposal = self._create_learning_proposal(RuleProposalVersion.ChangeEffect.RELAX)
        self.assertEqual(
            self._validation_post(proposal, RuleValidationRun.ValidationType.HISTORICAL_REPLAY).status_code,
            302,
        )
        self.assertEqual(self._validation_post(proposal, RuleValidationRun.ValidationType.SHADOW).status_code, 302)
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("governanceui:proposal-approve", args=[proposal.pk]),
            {"decision": RuleApprovalDecision.Decision.APPROVED, "rationale": "Admin must not relax."},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(proposal.approval_decisions.exists())

        self.client.force_login(self.system)
        response = self.client.post(
            reverse("governanceui:proposal-activate", args=[proposal.pk]),
            {"activation_scope": '{}', "effective_from": timezone.now().strftime("%Y-%m-%dT%H:%M")},
        )
        # Non-human principals cannot hold an interactive web session at all.
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PolicyActivation.objects.filter(rule_proposal_version=proposal).exists())
