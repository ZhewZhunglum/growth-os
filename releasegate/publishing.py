"""Human-confirmed, fail-closed publication dispatch boundary for Daily Operations V1.

This module is the only bridge between immutable release facts and a mutating
publication transport.  Networking is disabled unless deployment code injects
an explicit transport.  Tests use fakes only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from contentops.models import ContentAsset, ContentAssetVersion, TaskSubmission
from integrations.connectors.types import Platform
from integrations.publishing import (
    PublicationDispatchRequest,
    PublicationDispatchStatus,
    PublicationMode,
    PublicationRuntime,
)
from workflow.models import Task, TaskContractVersion
from workflow.exceptions import CheckGateRejected, IllegalTaskTransition
from workflow.services import guard_manual_publication, guard_release_gate

from .models import (
    AccountEnvironmentBinding,
    CapabilityState,
    ChannelAccount,
    PolicyDefinition,
    Publication,
    PublicationEvent,
    ReleaseGateRecord,
    RuntimeEnvironment,
)
from .services import record_manual_publication_proof


HUMAN_CONFIRMATION_TTL = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class HumanPublicationConfirmation:
    confirmation_id: uuid.UUID
    publication_id: uuid.UUID
    gate_id: uuid.UUID
    expected_publication_state_version: int
    gate_context_sha256: str
    publisher_principal_id: uuid.UUID
    acting_role: str
    permission_grant_id: uuid.UUID
    mode: PublicationMode
    confirmed_at: datetime


@dataclass(frozen=True, slots=True)
class ConfirmedPublicationResult:
    status: PublicationDispatchStatus
    mode: PublicationMode
    provider: str
    publication_event: PublicationEvent | None
    external_url: str = ""
    external_publication_id: str = ""
    reason: str = ""


def prepare_human_publication_confirmation(
    *,
    publication: Publication,
    publisher_principal: Principal,
    mode: PublicationMode,
    confirmation_id: uuid.UUID,
    confirmed: bool,
) -> HumanPublicationConfirmation:
    """Capture what the human explicitly confirmed; it performs no dispatch."""

    if confirmed is not True:
        raise ValidationError({"confirmed": "Final human publication confirmation is required."})
    try:
        confirmation_id = uuid.UUID(str(confirmation_id))
        mode = PublicationMode(mode)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("A valid confirmation_id and publication mode are required.") from exc
    publication = Publication.objects.select_related(
        "current_gate__publisher_grant",
        "requested_by_principal",
    ).get(pk=publication.pk)
    gate = publication.current_gate
    if publication.status not in {
        Publication.Status.READY_FOR_MANUAL_PUBLISH,
        Publication.Status.MANUAL_PUBLISHED_RECORDED,
    } or gate is None:
        raise ValidationError("Human confirmation requires the exact current READY Gate.")
    if publisher_principal.pk != publication.requested_by_principal_id:
        raise ValidationError("Only the exact authorized Publisher may confirm publication.")
    if gate.publisher_principal_id != publisher_principal.pk:
        raise ValidationError("The Gate Publisher no longer matches the confirming Principal.")
    publisher_principal.validate_acting_role(publication.requested_by_acting_role)
    return HumanPublicationConfirmation(
        confirmation_id=confirmation_id,
        publication_id=publication.pk,
        gate_id=gate.pk,
        expected_publication_state_version=publication.state_version,
        gate_context_sha256=gate.context_sha256,
        publisher_principal_id=publisher_principal.pk,
        acting_role=publication.requested_by_acting_role,
        permission_grant_id=gate.publisher_grant_id,
        mode=mode,
        confirmed_at=timezone.now(),
    )


def _validate_confirmation(
    *, publication: Publication, publisher: Principal, confirmation: HumanPublicationConfirmation
) -> ReleaseGateRecord:
    gate = publication.current_gate
    now = timezone.now()
    if confirmation.confirmed_at.tzinfo is None:
        raise ValidationError("Human confirmation timestamp must be timezone-aware.")
    if confirmation.confirmed_at > now or now - confirmation.confirmed_at > HUMAN_CONFIRMATION_TTL:
        raise ValidationError("Human publication confirmation is stale; confirm again.")
    if (
        confirmation.publication_id != publication.pk
        or confirmation.publisher_principal_id != publisher.pk
        or confirmation.acting_role != publication.requested_by_acting_role
        or confirmation.expected_publication_state_version != publication.state_version
        or gate is None
        or confirmation.gate_id != gate.pk
        or confirmation.gate_context_sha256 != gate.context_sha256
        or confirmation.permission_grant_id != gate.publisher_grant_id
    ):
        raise ValidationError("Human confirmation does not match the exact current release context.")
    publisher.validate_acting_role(confirmation.acting_role)
    return gate


def _lock_exact_release_context(publication_id: uuid.UUID) -> Publication:
    """Lock all mutable parents used by the immediate pre-dispatch re-check."""

    publication = Publication.objects.select_for_update().select_related(
        "submission__task__contract_version",
        "submission__primary_asset_version__content_asset",
        "current_gate__publisher_grant",
        "current_gate__channel_account",
        "current_gate__runtime_environment",
        "current_gate__account_environment_binding",
        "current_gate__capability_state",
        "requested_by_principal",
    ).get(pk=publication_id)
    gate = publication.current_gate
    if gate is None:
        return publication

    # Parent row locks serialize normal account, environment, capability,
    # policy, asset-version and submission changes against this last-mile
    # check.  The injected transport is called while these locks are held.
    Task.objects.select_for_update().get(pk=publication.submission.task_id)
    TaskSubmission.objects.select_for_update().get(pk=publication.submission_id)
    TaskContractVersion.objects.select_for_update().get(pk=gate.task_contract_version_id)
    ContentAsset.objects.select_for_update().get(pk=gate.primary_asset_version.content_asset_id)
    ContentAssetVersion.objects.select_for_update().get(pk=gate.primary_asset_version_id)
    Principal.objects.select_for_update().get(pk=gate.publisher_principal_id)
    PermissionGrant.objects.select_for_update().get(pk=gate.publisher_grant_id)
    ChannelAccount.objects.select_for_update().get(pk=gate.channel_account_id)
    RuntimeEnvironment.objects.select_for_update().get(pk=gate.runtime_environment_id)
    AccountEnvironmentBinding.objects.select_for_update().get(pk=gate.account_environment_binding_id)
    CapabilityState.objects.select_for_update().get(pk=gate.capability_state_id)
    list(PolicyDefinition.objects.select_for_update().values_list("pk", flat=True))
    return publication


def _pre_dispatch_blockers(publication: Publication, gate: ReleaseGateRecord) -> list[str]:
    blockers = list(gate.current_blockers())
    task = publication.submission.task
    try:
        guard_release_gate(
            task,
            submission=publication.submission,
            review_decision=gate.review_decision,
        )
        guard_manual_publication(task, publication=publication)
    except (ValidationError, CheckGateRejected, IllegalTaskTransition) as exc:
        # Workflow gate exceptions are deliberately converted to one
        # fail-closed reason without exposing a permissive fallback.
        blockers.append(f"WORKFLOW_CONTEXT_INVALID:{exc}")
    latest_gate = publication.gate_records.order_by("-evaluated_at", "-id").first()
    if latest_gate is None or latest_gate.pk != gate.pk:
        blockers.append("GATE_NOT_LATEST")
    latest_submission = task.submissions.order_by("-submission_number").first()
    if latest_submission is None or latest_submission.pk != publication.submission_id:
        blockers.append("SUBMISSION_NOT_LATEST")
    latest_asset = gate.primary_asset_version.content_asset.versions.order_by("-version_number").first()
    if latest_asset is None or latest_asset.pk != gate.primary_asset_version_id:
        blockers.append("PRIMARY_ASSET_VERSION_NOT_LATEST")
    return sorted(set(blockers))


@transaction.atomic
def dispatch_confirmed_publication(
    *,
    publication: Publication,
    publisher_principal: Principal,
    confirmation: HumanPublicationConfirmation,
    command_id: uuid.UUID,
    runtime: PublicationRuntime | None = None,
    manual_external_url: str = "",
    manual_external_publication_id: str = "",
) -> ConfirmedPublicationResult:
    """Re-check the exact current context, then perform one confirmed route.

    API and browser transports must be explicitly injected and idempotent on
    ``command_id``.  Manual mode never calls a transport.  A dry run never
    appends publication proof.
    """

    try:
        command_id = uuid.UUID(str(command_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError({"command_id": "A valid UUID command_id is required."}) from exc

    existing = PublicationEvent.objects.filter(command_id=command_id).select_related(
        "publication", "actor_principal"
    ).first()
    if existing is not None:
        if (
            existing.publication_id != publication.pk
            or existing.actor_principal_id != publisher_principal.pk
            or existing.event_type != PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED
        ):
            raise ValidationError("The command_id was already used by another publication action.")
        return ConfirmedPublicationResult(
            status=PublicationDispatchStatus.SUCCEEDED,
            mode=confirmation.mode,
            provider="recorded-idempotent-replay",
            publication_event=existing,
            external_url=existing.external_url,
            external_publication_id=existing.external_publication_id,
        )

    publication = _lock_exact_release_context(publication.pk)
    publisher = Principal.objects.get(pk=publisher_principal.pk)
    gate = _validate_confirmation(
        publication=publication,
        publisher=publisher,
        confirmation=confirmation,
    )
    blockers = _pre_dispatch_blockers(publication, gate)
    if blockers:
        raise ValidationError({"publication": f"Final publication re-check blocked: {', '.join(blockers)}"})

    try:
        platform = Platform(gate.channel_account.platform_code)
    except ValueError as exc:
        raise ValidationError("ChannelAccount platform is outside the seven-platform V1 scope.") from exc

    asset_reference = gate.primary_asset_version.object_key
    request = PublicationDispatchRequest(
        platform=platform,
        mode=confirmation.mode,
        operation_key=str(command_id),
        account_ref=gate.channel_account.account_code,
        asset_version_id=str(gate.primary_asset_version_id),
        asset_external_url=asset_reference,
        gate_id=str(gate.pk),
        gate_context_sha256=gate.context_sha256,
        human_confirmation_id=str(confirmation.confirmation_id),
        confirmed_by_principal_id=str(publisher.pk),
        metadata={
            "publication_id": str(publication.pk),
            "submission_id": str(gate.task_submission_id),
            "runtime_environment_id": str(gate.runtime_environment_id),
        },
    )

    if confirmation.mode is PublicationMode.MANUAL:
        if not (manual_external_url or manual_external_publication_id):
            raise ValidationError(
                "Manual publication requires the human-supplied external URL or content ID."
            )
        provider = f"{platform.value.lower()}-manual-proof"
        status = PublicationDispatchStatus.SUCCEEDED
        external_url = manual_external_url
        external_publication_id = manual_external_publication_id
        reason = ""
    else:
        dispatch_result = (runtime or PublicationRuntime()).dispatch(request)
        if dispatch_result.status is PublicationDispatchStatus.DRY_RUN:
            return ConfirmedPublicationResult(
                status=dispatch_result.status,
                mode=dispatch_result.mode,
                provider=dispatch_result.provider,
                publication_event=None,
                reason=dispatch_result.reason,
            )
        if dispatch_result.status is not PublicationDispatchStatus.SUCCEEDED:
            raise ValidationError(
                {"publication": f"Confirmed {confirmation.mode.value} dispatch failed closed: {dispatch_result.reason}"}
            )
        provider = dispatch_result.provider
        status = dispatch_result.status
        external_url = dispatch_result.external_url
        external_publication_id = dispatch_result.external_publication_id
        reason = dispatch_result.reason

    # The existing immutable proof writer repeats the PermissionGrant and Gate
    # checks and records actual Principal, acting role, exact Grant, and Gate.
    event = record_manual_publication_proof(
        publication=publication,
        publisher_principal=publisher,
        command_id=command_id,
        external_url=external_url,
        external_publication_id=external_publication_id,
    )
    return ConfirmedPublicationResult(
        status=status,
        mode=confirmation.mode,
        provider=provider,
        publication_event=event,
        external_url=external_url,
        external_publication_id=external_publication_id,
        reason=reason,
    )
