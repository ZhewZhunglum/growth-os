from __future__ import annotations

import uuid
from datetime import timedelta

from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase
from django.db.models.query import QuerySet
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from dailyops.evidence_services import invalidate_evidence
from dailyops.services import (
    ensure_default_sources,
    ingest_manual_link,
    propose_daily_analysis,
    start_daily_batch,
)
from integrations.connectors.types import Platform
from intelligence.models import EvidenceInvalidationEvent, ExternalEvidenceItem, canonical_sha256
from intelligence.admin import ImmutableAuditAdmin
from products.models import Product


class EvidenceInvalidationTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="evidence-remover",
            password="safe-local-password-123",
            role=Principal.Role.OWNER,
        )
        self.product = Product.objects.create(
            product_code="PUKO-EVIDENCE-REMOVE",
            name="PUKO Evidence Remove",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        now = timezone.now()
        for action, scope in (
            (PermissionGrant.Action.MANAGE_ACCOUNT, PermissionGrant.ScopeKind.GLOBAL),
            (PermissionGrant.Action.COLLECT_READ_ONLY, PermissionGrant.ScopeKind.GLOBAL),
            (PermissionGrant.Action.EDIT, PermissionGrant.ScopeKind.PRODUCT),
        ):
            PermissionGrant.objects.create(
                principal=self.owner,
                scope_kind=scope,
                product=self.product if scope == PermissionGrant.ScopeKind.PRODUCT else None,
                action=action,
                risk_level=(
                    PermissionGrant.RiskLevel.HIGH
                    if action == PermissionGrant.Action.MANAGE_ACCOUNT
                    else PermissionGrant.RiskLevel.LOW
                ),
                valid_from=now - timedelta(minutes=1),
                valid_until=now + timedelta(days=1),
                granted_by_principal=self.owner,
            )
        ensure_default_sources(principal=self.owner, acting_role=self.owner.role)
        self.batch_key = uuid.uuid4()
        start_daily_batch(
            batch_key=self.batch_key,
            product=self.product,
            query="afternoon focus",
            window_start=now - timedelta(days=7),
            window_end=now,
            principal=self.owner,
            acting_role=self.owner.role,
        )
        result = ingest_manual_link(
            batch_key=self.batch_key,
            product=self.product,
            platform=Platform.TIKTOK,
            operation_key=uuid.uuid4(),
            external_url="https://www.tiktok.com/@puko/video/123",
            external_content_id="",
            title="Afternoon focus question",
            content_text="People ask how to stay focused in the afternoon.",
            collected_at=now,
            principal=self.owner,
            acting_role=self.owner.role,
        )
        self.evidence = result.evidence[0]

    def test_removal_is_append_only_replay_safe_and_excluded_from_future_analysis(self):
        command_id = uuid.uuid4()
        first = invalidate_evidence(
            evidence_id=self.evidence.pk,
            product=self.product,
            batch_key=self.batch_key,
            command_id=command_id,
            reason="Pasted the wrong post.",
            principal=self.owner,
            acting_role=self.owner.role,
        )
        replay = invalidate_evidence(
            evidence_id=self.evidence.pk,
            product=self.product,
            batch_key=self.batch_key,
            command_id=command_id,
            reason="Pasted the wrong post.",
            principal=self.owner,
            acting_role=self.owner.role,
        )

        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertTrue(ExternalEvidenceItem.objects.filter(pk=self.evidence.pk).exists())
        self.assertEqual(EvidenceInvalidationEvent.objects.count(), 1)
        self.assertEqual(first.event.permission_grant.action, PermissionGrant.Action.EDIT)
        with self.assertRaisesMessage(ValidationError, "At least one provenance-linked evidence"):
            propose_daily_analysis(
                batch_key=self.batch_key,
                product=self.product,
                principal=self.owner,
                acting_role=self.owner.role,
            )

    def test_legacy_untagged_invalidation_command_replays_without_conflict(self):
        command_id = uuid.uuid4()
        legacy_reason = "Legacy untagged evidence reason."
        first = invalidate_evidence(
            evidence_id=self.evidence.pk,
            product=self.product,
            batch_key=self.batch_key,
            command_id=command_id,
            reason=legacy_reason,
            principal=self.owner,
            acting_role=self.owner.role,
        )
        QuerySet(model=EvidenceInvalidationEvent, using="default").filter(pk=first.event.pk).update(
            reason=legacy_reason,
            payload_hash=canonical_sha256(
                {
                    "evidence_item_id": str(self.evidence.pk),
                    "product_id": str(self.product.pk),
                    "reason": legacy_reason,
                }
            ),
        )

        replay = invalidate_evidence(
            evidence_id=self.evidence.pk,
            product=self.product,
            batch_key=self.batch_key,
            command_id=command_id,
            reason=legacy_reason,
            principal=self.owner,
            acting_role=self.owner.role,
        )

        self.assertFalse(replay.created)
        self.assertEqual(replay.event.pk, first.event.pk)

    def test_event_rejects_wrong_product(self):
        other = Product.objects.create(
            product_code="PUKO-OTHER",
            name="Other",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        # Authorization is checked before object ownership so callers cannot use
        # this endpoint to discover whether evidence belongs to another product.
        with self.assertRaisesMessage(PermissionDenied, "NO_ALLOW_GRANT"):
            invalidate_evidence(
                evidence_id=self.evidence.pk,
                product=other,
                batch_key=self.batch_key,
                command_id=uuid.uuid4(),
                reason="Wrong product.",
                principal=self.owner,
                acting_role=self.owner.role,
            )

    def test_event_rejects_evidence_from_another_batch(self):
        with self.assertRaisesMessage(ValidationError, "不属于本次分析"):
            invalidate_evidence(
                evidence_id=self.evidence.pk,
                product=self.product,
                batch_key=uuid.uuid4(),
                command_id=uuid.uuid4(),
                reason="Wrong batch.",
                principal=self.owner,
                acting_role=self.owner.role,
            )

    def test_audit_admin_cannot_add_invalidation_events(self):
        model_admin = ImmutableAuditAdmin(EvidenceInvalidationEvent, admin.site)
        request = RequestFactory().get("/admin/intelligence/evidenceinvalidationevent/add/")

        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))

    def test_direct_model_write_rejects_expired_grant_and_false_acting_role(self):
        now = timezone.now()
        expired_grant = PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
            valid_from=now - timedelta(days=2),
            valid_until=now - timedelta(days=1),
            granted_by_principal=self.owner,
        )
        common = {
            "evidence_item": self.evidence,
            "product": self.product,
            "command_id": uuid.uuid4(),
            "payload_hash": "",
            "reason": "Direct-write negative test.",
            "invalidated_by_principal": self.owner,
        }

        with self.assertRaisesMessage(ValidationError, "current active grant"):
            EvidenceInvalidationEvent.objects.create(
                **common,
                acting_role=self.owner.role,
                permission_grant=expired_grant,
            )

        current_grant = PermissionGrant.objects.get(
            principal=self.owner,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
            valid_until__gt=now,
        )
        with self.assertRaisesMessage(ValidationError, "acting role"):
            EvidenceInvalidationEvent.objects.create(
                **dict(common, command_id=uuid.uuid4()),
                acting_role=Principal.Role.OPERATOR,
                permission_grant=current_grant,
            )

    def test_direct_model_write_respects_deny_precedence(self):
        now = timezone.now()
        allow = PermissionGrant.objects.get(
            principal=self.owner,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
        )
        PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
            effect=PermissionGrant.Effect.DENY,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(days=1),
            granted_by_principal=self.owner,
        )

        with self.assertRaisesMessage(ValidationError, "fail-closed authorization"):
            EvidenceInvalidationEvent.objects.create(
                evidence_item=self.evidence,
                product=self.product,
                command_id=uuid.uuid4(),
                payload_hash="",
                reason="Deny precedence negative test.",
                invalidated_by_principal=self.owner,
                acting_role=self.owner.role,
                permission_grant=allow,
            )

    def test_replay_still_requires_current_authorization(self):
        command_id = uuid.uuid4()
        invalidate_evidence(
            evidence_id=self.evidence.pk,
            product=self.product,
            batch_key=self.batch_key,
            command_id=command_id,
            reason="Pasted the wrong post.",
            principal=self.owner,
            acting_role=self.owner.role,
        )
        outsider = Principal.objects.create_user(
            username="evidence-outsider",
            password="safe-local-password-456",
            role=Principal.Role.OPERATOR,
        )

        with self.assertRaisesMessage(PermissionDenied, "NO_ALLOW_GRANT"):
            invalidate_evidence(
                evidence_id=self.evidence.pk,
                product=self.product,
                batch_key=self.batch_key,
                command_id=command_id,
                reason="Pasted the wrong post.",
                principal=outsider,
                acting_role=outsider.role,
            )
