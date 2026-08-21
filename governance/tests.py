import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from insights.models import LearningVersion
from products.models import Product
from releasegate.models import PolicyDefinition, PolicyVersion

from .models import (
    Issue,
    PolicyActivation,
    PolicyActivationEvent,
    RuleApprovalDecision,
    RuleProposalSourceLink,
    RuleProposalVersion,
    RuleValidationRun,
)
from .services import activate_policy, transition_issue


class GovernanceLifecycleTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.owner = Principal.objects.create_user(
            username="governance-owner",
            password="safe-test-password-123",
            role=Principal.Role.OWNER,
        )
        self.admin = Principal.objects.create_user(
            username="governance-admin",
            password="safe-test-password-123",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        self.system = Principal.objects.create_user(
            username="governance-system",
            password="safe-test-password-123",
            role=Principal.Role.OPERATOR,
            principal_type=Principal.PrincipalType.SYSTEM,
        )
        self.product = Product.objects.create(
            product_code="PUKO-GOV",
            name="PUKO Governance",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.owner_approve = self.grant(self.owner, PermissionGrant.Action.APPROVE)
        self.owner_edit = self.grant(self.owner, PermissionGrant.Action.EDIT)
        self.admin_approve = self.grant(self.admin, PermissionGrant.Action.APPROVE)
        self.system_approve = self.grant(self.system, PermissionGrant.Action.APPROVE)
        self.definition = PolicyDefinition.objects.create(
            policy_code="puko-claims",
            name="PUKO claims",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.previous_policy = PolicyVersion.objects.create(
            policy_definition=self.definition,
            version_number=1,
            rules=[{"rule_code": "claim-safe"}],
            effective_from=self.now - timedelta(days=2),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self.candidate_policy = PolicyVersion.objects.create(
            policy_definition=self.definition,
            version_number=2,
            rules=[{"rule_code": "claim-safe-relaxed"}],
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

    def proposal(self, effect=RuleProposalVersion.ChangeEffect.RELAX):
        return RuleProposalVersion.objects.create(
            proposal_key=f"proposal-{effect.lower()}",
            version_number=1,
            target_policy_definition=self.definition,
            candidate_policy_version=self.candidate_policy,
            change_effect=effect,
            risk_level=RuleProposalVersion.RiskLevel.HIGH,
            rationale="Bounded rule change.",
            created_by_principal=self.owner,
            permission_grant=self.owner_edit,
        )

    def test_relax_requires_owner_approval(self):
        proposal = self.proposal()
        with self.assertRaises(ValidationError):
            RuleApprovalDecision.objects.create(
                rule_proposal_version=proposal,
                decision=RuleApprovalDecision.Decision.APPROVED,
                approver_principal=self.admin,
                acting_role="OPERATIONS_ADMIN",
                permission_grant=self.admin_approve,
                rationale="Admin attempts to relax.",
            )
        decision = RuleApprovalDecision.objects.create(
            rule_proposal_version=proposal,
            decision=RuleApprovalDecision.Decision.APPROVED,
            approver_principal=self.owner,
            acting_role="OWNER",
            permission_grant=self.owner_approve,
            rationale="Owner explicitly approves the relaxation.",
        )
        self.assertEqual(decision.approver_principal_id, self.owner.pk)

    def test_learning_source_cannot_auto_activate(self):
        learning = LearningVersion.objects.create(
            learning_key="learning-proposal-only",
            version_number=1,
            product=self.product,
            title="Learning proposal",
            conclusion="Bounded observation.",
            recommended_action="Propose a policy review.",
            confidence=Decimal("0.7000"),
            created_by_principal=self.owner,
        )
        proposal = self.proposal(RuleProposalVersion.ChangeEffect.TIGHTEN)
        RuleProposalSourceLink.objects.create(
            rule_proposal_version=proposal,
            source_kind=RuleProposalSourceLink.SourceKind.LEARNING,
            learning_version=learning,
            permission_grant=self.owner_edit,
        )

        replay = self.validation(
            proposal,
            RuleValidationRun.ValidationType.HISTORICAL_REPLAY,
            self.now,
        )
        shadow = self.validation(
            proposal,
            RuleValidationRun.ValidationType.SHADOW,
            replay.completed_at + timedelta(minutes=1),
        )

        with self.assertRaises(ValidationError):
            RuleApprovalDecision.objects.create(
                rule_proposal_version=proposal,
                decision=RuleApprovalDecision.Decision.APPROVED,
                approver_principal=self.system,
                acting_role="OPERATOR",
                permission_grant=self.system_approve,
                rationale="A system account must not approve Learning automatically.",
                decided_at=shadow.completed_at + timedelta(minutes=1),
            )

        approval = RuleApprovalDecision.objects.create(
            rule_proposal_version=proposal,
            decision=RuleApprovalDecision.Decision.APPROVED,
            approver_principal=self.owner,
            acting_role="OWNER",
            permission_grant=self.owner_approve,
            rationale="Owner explicitly approves a bounded canary.",
            decided_at=shadow.completed_at + timedelta(minutes=1),
        )
        canary = self.validation(
            proposal,
            RuleValidationRun.ValidationType.CANARY,
            approval.decided_at + timedelta(minutes=1),
        )

        with self.assertRaises(ValidationError):
            activate_policy(
                proposal=proposal,
                activation_scope={"market": "US"},
                effective_from=canary.completed_at,
                command_id=uuid.uuid4(),
                actor_principal=self.system,
                acting_role="OPERATOR",
                permission_grant=self.system_approve,
            )
        with self.assertRaises(ValidationError):
            PolicyActivation.objects.create(
                rule_proposal_version=proposal,
                policy_version=self.candidate_policy,
                activation_scope={"market": "US"},
                effective_from=self.now,
                activated_by_principal=self.owner,
                acting_role="OWNER",
                permission_grant=self.owner_approve,
            )
        self.assertFalse(PolicyActivation.objects.filter(rule_proposal_version=proposal).exists())

        activation = activate_policy(
            proposal=proposal,
            activation_scope={"market": "US"},
            effective_from=canary.completed_at,
            command_id=uuid.uuid4(),
            actor_principal=self.owner,
            acting_role="OWNER",
            permission_grant=self.owner_approve,
        )
        self.assertEqual(activation.rule_proposal_version_id, proposal.pk)

    def test_activation_needs_ordered_validation_and_is_append_only(self):
        proposal = self.proposal(RuleProposalVersion.ChangeEffect.TIGHTEN)
        replay = self.validation(proposal, RuleValidationRun.ValidationType.HISTORICAL_REPLAY, self.now)
        shadow = self.validation(
            proposal,
            RuleValidationRun.ValidationType.SHADOW,
            replay.completed_at + timedelta(minutes=1),
        )
        approval = RuleApprovalDecision.objects.create(
            rule_proposal_version=proposal,
            decision=RuleApprovalDecision.Decision.APPROVED,
            approver_principal=self.owner,
            acting_role="OWNER",
            permission_grant=self.owner_approve,
            rationale="Approve bounded canary.",
            decided_at=shadow.completed_at + timedelta(minutes=1),
        )
        canary = self.validation(
            proposal,
            RuleValidationRun.ValidationType.CANARY,
            approval.decided_at + timedelta(minutes=1),
        )
        activation = activate_policy(
            proposal=proposal,
            activation_scope={"market": "US"},
            effective_from=canary.completed_at,
            command_id=uuid.uuid4(),
            actor_principal=self.owner,
            acting_role="OWNER",
            permission_grant=self.owner_approve,
        )
        self.assertEqual(activation.events.get().event_type, PolicyActivationEvent.EventType.ACTIVATED)
        activation.activation_scope = {"market": "ALL"}
        with self.assertRaises(ValidationError):
            activation.save()

    def validation(self, proposal, validation_type, started_at):
        return RuleValidationRun.objects.create(
            rule_proposal_version=proposal,
            validation_type=validation_type,
            policy_version=self.candidate_policy,
            input_version_hash=(validation_type.lower().replace("_", "-") + "0" * 64)[:64],
            data_window_start=self.now - timedelta(days=30),
            data_window_end=self.now - timedelta(days=1),
            result=RuleValidationRun.Result.PASSED,
            started_at=started_at,
            completed_at=started_at + timedelta(minutes=1),
            created_by_principal=self.owner,
        )

    def test_issue_state_is_event_projected_and_idempotent(self):
        issue = Issue.objects.create(
            issue_key="issue-connector-blocked",
            issue_type=Issue.IssueType.BLOCKER,
            severity=Issue.Severity.HIGH,
            title="Connector login expired",
            description="Browser worker needs a human login.",
            created_by_principal=self.owner,
            permission_grant=self.owner_edit,
        )
        command_id = uuid.uuid4()
        event = transition_issue(
            issue=issue,
            to_state=Issue.State.TRIAGED,
            reason="Owner acknowledged the login blocker.",
            command_id=command_id,
            expected_state_version=0,
            actor_principal=self.owner,
            acting_role="OWNER",
            permission_grant=self.owner_edit,
        )
        replay = transition_issue(
            issue=issue,
            to_state=Issue.State.TRIAGED,
            reason="Owner acknowledged the login blocker.",
            command_id=command_id,
            expected_state_version=0,
            actor_principal=self.owner,
            acting_role="OWNER",
            permission_grant=self.owner_edit,
        )
        self.assertEqual(replay.pk, event.pk)
        issue.refresh_from_db()
        self.assertEqual(issue.current_state, Issue.State.TRIAGED)
        self.assertEqual(issue.state_version, 1)

    def test_governance_creation_rejects_a_wrong_exact_grant(self):
        with self.assertRaises(ValidationError):
            Issue.objects.create(
                issue_key="issue-wrong-creation-grant",
                issue_type=Issue.IssueType.OPERATIONAL,
                severity=Issue.Severity.LOW,
                title="Wrong grant",
                description="APPROVE must not authorize Issue creation.",
                created_by_principal=self.owner,
                permission_grant=self.owner_approve,
            )

        with self.assertRaises(ValidationError):
            RuleProposalVersion.objects.create(
                proposal_key="proposal-wrong-creation-grant",
                version_number=1,
                target_policy_definition=self.definition,
                candidate_policy_version=self.candidate_policy,
                change_effect=RuleProposalVersion.ChangeEffect.TIGHTEN,
                risk_level=RuleProposalVersion.RiskLevel.HIGH,
                rationale="APPROVE must not authorize proposal creation.",
                created_by_principal=self.owner,
                permission_grant=self.owner_approve,
            )
