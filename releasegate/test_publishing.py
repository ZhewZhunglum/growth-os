from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant
from contentops.models import ContentAssetVersion
from integrations.connectors.types import Platform
from integrations.publishing import (
    DryRunPublicationTransport,
    PublicationDispatchResult,
    PublicationDispatchStatus,
    PublicationMode,
    PublicationRuntime,
    PublicationRuntimeConfig,
)

from .models import CapabilityState, Publication, PublicationEvent
from .publishing import (
    dispatch_confirmed_publication,
    prepare_human_publication_confirmation,
)
from .services import orchestrate_v1_release_gate
from . import tests as releasegate_tests


class _ReleaseFixture:
    _grant = releasegate_tests.ReleaseGateDomainTests._grant


class _SuccessfulTransport:
    def __init__(self):
        self.calls = []

    def dispatch(self, request):
        self.calls.append(request)
        return PublicationDispatchResult(
            platform=request.platform,
            mode=request.mode,
            provider="fake-tiktok-content-posting-api",
            status=PublicationDispatchStatus.SUCCEEDED,
            operation_key=request.operation_key,
            external_url="https://www.tiktok.com/@puko/video/confirmed-123",
            external_publication_id="confirmed-123",
        )


class HumanConfirmedPublicationTests(TestCase):
    def setUp(self):
        fixture = _ReleaseFixture()
        create_next = ContentAssetVersion.create_next

        def link_only_create_next(**kwargs):
            kwargs["object_key"] = "https://drafts.example.com/puko/tiktok-v1"
            kwargs["mime_type"] = "text/uri-list"
            return create_next(**kwargs)

        with patch.object(ContentAssetVersion, "create_next", side_effect=link_only_create_next):
            releasegate_tests.ReleaseGateDomainTests.setUp(fixture)
        self.fixture = fixture
        result = orchestrate_v1_release_gate(
            task=fixture.task,
            submission=fixture.submission,
            publisher_principal=fixture.publisher,
            channel_account=fixture.channel_account,
            runtime_environment=fixture.environment,
            command_id=uuid.uuid4(),
        )
        self.publication = result.publication

    def confirmation(self, mode=PublicationMode.API):
        return prepare_human_publication_confirmation(
            publication=self.publication,
            publisher_principal=self.fixture.publisher,
            mode=mode,
            confirmation_id=uuid.uuid4(),
            confirmed=True,
        )

    def runtime(self, transport):
        return PublicationRuntime(
            PublicationRuntimeConfig({(Platform.TIKTOK, PublicationMode.API): transport})
        )

    def test_final_human_confirmation_is_mandatory(self):
        with self.assertRaises(ValidationError):
            prepare_human_publication_confirmation(
                publication=self.publication,
                publisher_principal=self.fixture.publisher,
                mode=PublicationMode.API,
                confirmation_id=uuid.uuid4(),
                confirmed=False,
            )

    def test_explicit_fake_api_dispatch_rechecks_and_records_exact_immutable_proof(self):
        transport = _SuccessfulTransport()
        command_id = uuid.uuid4()
        confirmation = self.confirmation()
        result = dispatch_confirmed_publication(
            publication=self.publication,
            publisher_principal=self.fixture.publisher,
            confirmation=confirmation,
            command_id=command_id,
            runtime=self.runtime(transport),
        )
        self.assertEqual(result.status, PublicationDispatchStatus.SUCCEEDED)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result.publication_event.actor_principal_id, self.fixture.publisher.pk)
        self.assertEqual(result.publication_event.permission_grant_id, self.fixture.publish_grant.pk)
        self.assertEqual(result.publication_event.release_gate_id, confirmation.gate_id)
        self.assertEqual(
            result.publication_event.event_type,
            PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED,
        )

        replay = dispatch_confirmed_publication(
            publication=self.publication,
            publisher_principal=self.fixture.publisher,
            confirmation=confirmation,
            command_id=command_id,
            runtime=self.runtime(transport),
        )
        self.assertEqual(replay.publication_event.pk, result.publication_event.pk)
        self.assertEqual(len(transport.calls), 1)

    def test_default_disabled_runtime_fails_without_calling_or_recording_proof(self):
        before = self.publication.events.count()
        with self.assertRaises(ValidationError):
            dispatch_confirmed_publication(
                publication=self.publication,
                publisher_principal=self.fixture.publisher,
                confirmation=self.confirmation(),
                command_id=uuid.uuid4(),
            )
        self.publication.refresh_from_db()
        self.assertEqual(self.publication.status, Publication.Status.READY_FOR_MANUAL_PUBLISH)
        self.assertEqual(self.publication.events.count(), before)

    def test_dry_run_never_records_external_proof(self):
        before = self.publication.events.count()
        runtime = self.runtime(DryRunPublicationTransport())
        result = dispatch_confirmed_publication(
            publication=self.publication,
            publisher_principal=self.fixture.publisher,
            confirmation=self.confirmation(),
            command_id=uuid.uuid4(),
            runtime=runtime,
        )
        self.assertEqual(result.status, PublicationDispatchStatus.DRY_RUN)
        self.assertIsNone(result.publication_event)
        self.publication.refresh_from_db()
        self.assertEqual(self.publication.status, Publication.Status.READY_FOR_MANUAL_PUBLISH)
        self.assertEqual(self.publication.events.count(), before)

    def test_manual_fallback_records_human_supplied_url_without_transport(self):
        result = dispatch_confirmed_publication(
            publication=self.publication,
            publisher_principal=self.fixture.publisher,
            confirmation=self.confirmation(PublicationMode.MANUAL),
            command_id=uuid.uuid4(),
            manual_external_url="https://www.tiktok.com/@puko/video/manual-1",
            manual_external_publication_id="manual-1",
        )
        self.assertEqual(result.status, PublicationDispatchStatus.SUCCEEDED)
        self.assertEqual(result.publication_event.external_publication_id, "manual-1")

    def test_capability_change_after_confirmation_blocks_before_transport(self):
        confirmation = self.confirmation()
        CapabilityState.objects.create(
            account_environment_binding=self.fixture.binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=2,
            state=CapabilityState.State.CLOSED,
            supersedes=self.fixture.capability,
            reason="Emergency stop before dispatch",
            created_by_principal=self.fixture.owner,
            recorded_by_principal=self.fixture.owner,
        )
        transport = _SuccessfulTransport()
        with self.assertRaises(ValidationError):
            dispatch_confirmed_publication(
                publication=self.publication,
                publisher_principal=self.fixture.publisher,
                confirmation=confirmation,
                command_id=uuid.uuid4(),
                runtime=self.runtime(transport),
            )
        self.assertEqual(transport.calls, [])

    def test_publish_grant_revocation_after_confirmation_blocks_before_transport(self):
        confirmation = self.confirmation()
        grant = self.fixture.publish_grant
        grant.grant_status = PermissionGrant.GrantStatus.REVOKED
        grant.revoked_at = timezone.now()
        grant.revoked_by_principal = self.fixture.owner
        grant.revocation_reason = "Emergency revoke before dispatch"
        grant.save()
        transport = _SuccessfulTransport()
        with self.assertRaises(ValidationError):
            dispatch_confirmed_publication(
                publication=self.publication,
                publisher_principal=self.fixture.publisher,
                confirmation=confirmation,
                command_id=uuid.uuid4(),
                runtime=self.runtime(transport),
            )
        self.assertEqual(transport.calls, [])

    def test_new_asset_version_makes_old_gate_fail_closed_before_transport(self):
        confirmation = self.confirmation()
        ContentAssetVersion.create_next(
            content_asset=self.fixture.asset,
            object_key="https://drafts.example.com/puko/tiktok-v2",
            mime_type="text/uri-list",
            byte_size=43,
            content_sha256="b" * 64,
            command_id=uuid.uuid4(),
            actor_principal=self.fixture.owner,
            acting_role=self.fixture.owner.role,
            permission_grant=self.fixture.edit_grant,
            recorded_by_principal=self.fixture.owner,
        )
        transport = _SuccessfulTransport()
        with self.assertRaises(ValidationError) as caught:
            dispatch_confirmed_publication(
                publication=self.publication,
                publisher_principal=self.fixture.publisher,
                confirmation=confirmation,
                command_id=uuid.uuid4(),
                runtime=self.runtime(transport),
            )
        self.assertIn("PRIMARY_ASSET_VERSION_NOT_LATEST", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_stale_or_wrong_principal_confirmation_is_rejected(self):
        confirmation = self.confirmation()
        stale = replace(confirmation, confirmed_at=timezone.now() - timedelta(minutes=6))
        with self.assertRaises(ValidationError):
            dispatch_confirmed_publication(
                publication=self.publication,
                publisher_principal=self.fixture.publisher,
                confirmation=stale,
                command_id=uuid.uuid4(),
                runtime=self.runtime(_SuccessfulTransport()),
            )
        with self.assertRaises(ValidationError):
            dispatch_confirmed_publication(
                publication=self.publication,
                publisher_principal=self.fixture.owner,
                confirmation=confirmation,
                command_id=uuid.uuid4(),
                runtime=self.runtime(_SuccessfulTransport()),
            )
