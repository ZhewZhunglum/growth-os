from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable, Mapping

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.authorization import require_authorization
from accounts.models import PermissionGrant, Principal
from core.ids import uuid7
from integrations.ai.providers import AIProvider
from integrations.ai.types import AIMessage, AIRequest, StructuredOutputSpec
from integrations.connectors.catalog import default_connector_catalog
from integrations.connectors.types import (
    AcquisitionMode,
    ConnectorRequest,
    ConnectorResult,
    ConnectorRunStatus,
    Platform,
)
from integrations.ingestion import (
    CSVIngestionValidator,
    ManualEvidenceInput,
    validate_connector_evidence,
    validate_manual_evidence,
)
from intelligence.models import (
    AssessmentMethod,
    AvailabilityState,
    ChannelPlan,
    ChannelPlanStateEvent,
    CollectionRun,
    DecisionState,
    DemandAssessment,
    DemandEvidenceLink,
    EvidenceArtifactLink,
    ExternalEvidenceItem,
    Initiative,
    InitiativeStateEvent,
    ProductOpportunity,
    ProductTopicFit,
    ProductTopicFitAssessment,
    RawArtifact,
    RiskLevel,
    SignalAssessment,
    SourceRegistry,
    TaskCompilationContext,
    Topic,
    TopicEvidenceLink,
    canonical_sha256,
)
from intelligence.exceptions import CommandReplayConflict
from intelligence.services import (
    record_collection_run,
    transition_channel_plan,
    transition_initiative,
    transition_opportunity,
)
from products.models import Product
from releasegate.models import AccountEnvironmentBinding, CapabilityState
from workflow.models import Task, TaskContractVersion

from dailyops.runtime import DailyOperationsRuntime, DailyOperationsRuntimeConfig, build_daily_operations_runtime
from dailyops.schemas import DAILY_ANALYSIS_SCHEMA, deterministic_analysis


PLATFORMS: tuple[Platform, ...] = tuple(Platform)
MAX_ANALYSIS_EVIDENCE = 100
PRIMARY_MODE: dict[Platform, AcquisitionMode] = {
    **{platform: AcquisitionMode.API for platform in PLATFORMS},
    Platform.QUORA: AcquisitionMode.BROWSER,
}
SOURCE_KIND_BY_MODE = {
    AcquisitionMode.API: SourceRegistry.SourceKind.THIRD_PARTY_API,
    AcquisitionMode.BROWSER: SourceRegistry.SourceKind.BROWSER,
    AcquisitionMode.CSV: SourceRegistry.SourceKind.CSV,
    AcquisitionMode.MANUAL: SourceRegistry.SourceKind.MANUAL_LINK,
}


@dataclass(frozen=True, slots=True)
class DailyBatchResult:
    batch_key: uuid.UUID
    runs: tuple[CollectionRun, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class EvidenceIngestionResult:
    run: CollectionRun
    evidence: tuple[ExternalEvidenceItem, ...]
    created_count: int


@dataclass(frozen=True, slots=True)
class CompiledTaskResult:
    task: Task
    context: TaskCompilationContext
    created: bool


@dataclass(frozen=True, slots=True)
class AutomaticCollectionResult:
    command_id: uuid.UUID
    runs: tuple[CollectionRun, ...]
    evidence: tuple[ExternalEvidenceItem, ...]
    created_count: int
    created: bool


@dataclass(frozen=True, slots=True)
class PlatformCollectionResult:
    command_id: uuid.UUID
    platform: Platform
    run: CollectionRun
    evidence: tuple[ExternalEvidenceItem, ...]
    created_count: int
    created: bool


@dataclass(frozen=True, slots=True)
class StartedExecutionProjectResult:
    """The separate immutable facts produced by the Owner shortcut."""

    opportunity: ProductOpportunity
    initiative: Initiative


@dataclass(frozen=True, slots=True)
class ConfirmedPlanTaskResult:
    """An activated plan plus its exact sealed task compilation result."""

    channel_plan: ChannelPlan
    compilation: CompiledTaskResult


def _source_key(platform: Platform, mode: AcquisitionMode) -> str:
    return f"daily-{platform.value.lower()}-{mode.value.lower()}".replace("_", "-")


def _latest_source(platform: Platform, mode: AcquisitionMode) -> SourceRegistry:
    source = (
        SourceRegistry.objects.filter(
            source_key=_source_key(platform, mode),
            platform_code=platform.value,
            source_kind=SOURCE_KIND_BY_MODE[mode],
            status=SourceRegistry.Status.ACTIVE,
        )
        .order_by("-version_number")
        .first()
    )
    if source is None:
        raise ValidationError(
            f"{platform.value}/{mode.value} source is not configured. Configure Sources before starting Daily Operations."
        )
    return source


def ensure_default_sources(*, principal: Principal, acting_role: str) -> tuple[SourceRegistry, ...]:
    """Create only non-secret source descriptors through an explicitly authorized setup action."""

    require_authorization(
        principal=principal,
        acting_role=acting_role,
        action=PermissionGrant.Action.MANAGE_ACCOUNT,
        scope_kind=PermissionGrant.ScopeKind.GLOBAL,
    )
    catalog = default_connector_catalog()
    sources: list[SourceRegistry] = []
    with transaction.atomic():
        for platform in PLATFORMS:
            descriptor = catalog[platform]
            routes = {route.mode: route for route in descriptor.routes}
            for mode in AcquisitionMode:
                key = _source_key(platform, mode)
                existing = SourceRegistry.objects.filter(source_key=key).order_by("-version_number").first()
                expected_config = {
                    "provider": routes[mode].provider,
                    "mode": mode.value,
                    "priority": routes[mode].priority,
                    "contains_secret": False,
                }
                if existing:
                    if (
                        existing.platform_code != platform.value
                        or existing.source_kind != SOURCE_KIND_BY_MODE[mode]
                        or existing.non_secret_config != expected_config
                    ):
                        raise ValidationError(
                            f"Existing SourceRegistry {key} differs from the frozen connector descriptor; create a new version."
                        )
                    sources.append(existing)
                    continue
                sources.append(
                    SourceRegistry.objects.create(
                        source_key=key,
                        version_number=1,
                        platform_code=platform.value,
                        source_kind=SOURCE_KIND_BY_MODE[mode],
                        display_name=f"{platform.value} {mode.value} route",
                        trust_tier=2 if mode in {AcquisitionMode.CSV, AcquisitionMode.MANUAL} else 3,
                        non_secret_config=expected_config,
                        created_by_principal=principal,
                    )
                )
    return tuple(sources)


def _assert_batch_product(runs: Iterable[CollectionRun], product: Product) -> tuple[CollectionRun, ...]:
    materialized = tuple(runs)
    if not materialized:
        raise ValidationError("Daily batch was not found.")
    expected = str(product.pk)
    if any(str(run.query_spec.get("product_id", "")) != expected for run in materialized):
        raise ValidationError("Daily batch contains a run for another Product.")
    return materialized


def batch_runs(*, batch_key: uuid.UUID, product: Product) -> tuple[CollectionRun, ...]:
    return _assert_batch_product(
        CollectionRun.objects.filter(batch_key=batch_key).select_related("source").order_by(
            "source__platform_code", "source__source_kind", "attempt_number"
        ),
        product,
    )


def start_daily_batch(
    *,
    batch_key: uuid.UUID,
    product: Product,
    query: str,
    window_start,
    window_end,
    principal: Principal,
    acting_role: str,
) -> DailyBatchResult:
    """Record one explicit initial attempt for every V1 platform.

    No connector is called here. Until a live API/browser route is explicitly
    enabled, every platform is recorded as needing CSV/manual/browser input.
    """

    query = query.strip()
    if not query:
        raise ValidationError({"query": "Daily collection query is required."})
    if window_start.tzinfo is None or window_end.tzinfo is None or window_end <= window_start:
        raise ValidationError("Daily collection requires a valid timezone-aware window.")
    if product.product_status != Product.ProductStatus.ACTIVE:
        raise ValidationError("Daily Operations can run only for an ACTIVE Product.")
    existing = tuple(CollectionRun.objects.filter(batch_key=batch_key).select_related("source"))
    if existing:
        _assert_batch_product(existing, product)
        if any(
            run.query_spec.get("query") != query
            or run.query_spec.get("window_start") != window_start.isoformat()
            or run.query_spec.get("window_end") != window_end.isoformat()
            for run in existing
        ):
            raise ValidationError("This batch key was already used for different Daily Operations input.")
        return DailyBatchResult(batch_key=batch_key, runs=existing, created=False)

    catalog = default_connector_catalog()
    now = timezone.now()
    created_runs: list[CollectionRun] = []
    with transaction.atomic():
        for platform in PLATFORMS:
            primary_mode = PRIMARY_MODE[platform]
            source = _latest_source(platform, primary_mode)
            descriptor = catalog[platform]
            routes = [
                {
                    "mode": route.mode.value,
                    "state": route.state.value,
                    "provider": route.provider,
                    "reason": route.reason,
                }
                for route in descriptor.routes
            ]
            query_spec = {
                "product_id": str(product.pk),
                "query": query,
                "market_code": product.market_code,
                "language_code": product.language_code,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "requested_primary_mode": primary_mode.value,
            }
            operation_key = uuid.uuid5(batch_key, f"initial:{source.pk}:1")
            outcome = record_collection_run(
                source=source,
                batch_key=batch_key,
                attempt_number=1,
                operation_key=operation_key,
                query_spec=query_spec,
                status=CollectionRun.Status.BLOCKED,
                availability_state=AvailabilityState.MISSING,
                started_at=now,
                completed_at=now,
                result_summary={"items": 0, "routes": routes, "next_step": "PROVIDE_FALLBACK_INPUT"},
                error_code="CONNECTOR_INPUT_OR_CREDENTIALS_REQUIRED",
                principal=principal,
                acting_role=acting_role,
            )
            created_runs.append(outcome.run)
    return DailyBatchResult(batch_key=batch_key, runs=tuple(created_runs), created=True)


def _next_attempt(*, batch_key: uuid.UUID, source: SourceRegistry) -> int:
    current = (
        CollectionRun.objects.filter(batch_key=batch_key, source=source)
        .order_by("-attempt_number")
        .values_list("attempt_number", flat=True)
        .first()
    )
    return (current or 0) + 1


def _persist_ingested_evidence(*, run: CollectionRun, source: SourceRegistry, item, principal: Principal):
    raw, raw_created = RawArtifact.objects.get_or_create(
        dedupe_key=item.dedupe_key,
        defaults={
            "collection_run": run,
            "source": source,
            "external_url": item.url or "",
            "external_content_id": item.external_id or "",
            "media_type": "application/vnd.growth-os.link+json",
            "observed_at": item.provenance.collected_at,
            "payload": {
                "title": item.title,
                "content_text": item.content_text,
                "query": item.query,
                "attributes": dict(item.attributes),
            },
            "content_sha256": "",
        },
    )
    evidence, evidence_created = ExternalEvidenceItem.objects.get_or_create(
        dedupe_key=item.dedupe_key,
        defaults={
            "source": source,
            "collection_run": run,
            "platform_code": item.platform.value,
            "market_code": item.market_code or run.query_spec.get("market_code", ""),
            "language_code": item.language_code or run.query_spec.get("language_code", ""),
            "external_url": item.url or "",
            "external_content_id": item.external_id or "",
            "title": item.title,
            "excerpt": item.content_text,
            "facts": {"query": item.query, "attributes": dict(item.attributes)},
            "observed_at": item.provenance.collected_at,
            "provenance_sha256": "",
            "created_by_principal": principal,
        },
    )
    if raw_created != evidence_created:
        raise ValidationError("Evidence dedupe state is incomplete; manual repair is required.")
    if raw.collection_run_id != evidence.collection_run_id:
        raise ValidationError("Duplicate evidence belongs to another immutable collection run.")
    EvidenceArtifactLink.objects.get_or_create(
        evidence_item=evidence,
        raw_artifact=raw,
        defaults={"created_by_principal": principal},
    )
    return evidence, evidence_created


def _record_successful_fallback(
    *, batch_key, product, source, operation_key, items, principal, acting_role
) -> EvidenceIngestionResult:
    requested_dedupe = sorted(item.dedupe_key for item in items)
    replay_run = CollectionRun.objects.filter(operation_key=operation_key).first()
    if replay_run is not None:
        if replay_run.batch_key != batch_key or replay_run.source_id != source.pk:
            raise ValidationError("The operation key was already used for another fallback collection.")
        replay_evidence = tuple(
            ExternalEvidenceItem.objects.filter(dedupe_key__in=requested_dedupe).order_by("dedupe_key")
        )
        if [item.dedupe_key for item in replay_evidence] != requested_dedupe:
            raise ValidationError("The operation key was replayed with different evidence.")
        return EvidenceIngestionResult(replay_run, replay_evidence, 0)
    original_runs = _assert_batch_product(
        CollectionRun.objects.filter(batch_key=batch_key).order_by("started_at", "id"), product
    )
    attempt = _next_attempt(batch_key=batch_key, source=source)
    collected_at = min(item.provenance.collected_at for item in items)
    completed_at = max(item.provenance.collected_at for item in items)
    query_spec = dict(original_runs[0].query_spec)
    query_spec.update(
        {
            "fallback_mode": items[0].provenance.acquisition_mode.value,
            "item_count": len(items),
            "evidence_dedupe_keys": requested_dedupe,
        }
    )
    result = record_collection_run(
        source=source,
        batch_key=batch_key,
        attempt_number=attempt,
        operation_key=operation_key,
        query_spec=query_spec,
        status=CollectionRun.Status.SUCCEEDED,
        availability_state=AvailabilityState.PRESENT,
        started_at=collected_at,
        completed_at=completed_at,
        result_summary={"items": len(items), "mode": items[0].provenance.acquisition_mode.value},
        error_code="",
        principal=principal,
        acting_role=acting_role,
    )
    evidence: list[ExternalEvidenceItem] = []
    created_count = 0
    for item in items:
        persisted, created = _persist_ingested_evidence(
            run=result.run, source=source, item=item, principal=principal
        )
        evidence.append(persisted)
        created_count += int(created)
    return EvidenceIngestionResult(result.run, tuple(evidence), created_count)


@transaction.atomic
def ingest_manual_link(
    *,
    batch_key: uuid.UUID,
    product: Product,
    platform: Platform,
    operation_key: uuid.UUID,
    external_url: str,
    external_content_id: str,
    title: str,
    content_text: str,
    collected_at,
    principal: Principal,
    acting_role: str,
) -> EvidenceIngestionResult:
    source = _latest_source(platform, AcquisitionMode.MANUAL)
    item = validate_manual_evidence(
        ManualEvidenceInput(
            platform=platform,
            source_key=source.source_key,
            collection_run_key=batch_key.hex,
            collected_by=str(principal.pk),
            collected_at=collected_at,
            external_id=external_content_id or None,
            url=external_url or None,
            title=title,
            content_text=content_text,
            language_code=product.language_code,
            market_code=product.market_code,
            query="",
            attributes={"product_id": str(product.pk)},
        )
    )
    return _record_successful_fallback(
        batch_key=batch_key,
        product=product,
        source=source,
        operation_key=operation_key,
        items=(item,),
        principal=principal,
        acting_role=acting_role,
    )


@transaction.atomic
def ingest_csv_text(
    *,
    batch_key: uuid.UUID,
    product: Product,
    platform: Platform,
    operation_key: uuid.UUID,
    csv_text: str,
    principal: Principal,
    acting_role: str,
) -> EvidenceIngestionResult:
    source = _latest_source(platform, AcquisitionMode.CSV)
    items = CSVIngestionValidator().validate(
        csv_text,
        platform=platform,
        source_key=source.source_key,
        collection_run_key=batch_key.hex,
        collected_by=str(principal.pk),
    )
    return _record_successful_fallback(
        batch_key=batch_key,
        product=product,
        source=source,
        operation_key=operation_key,
        items=items,
        principal=principal,
        acting_role=acting_role,
    )


def _automatic_operation_id(command_id: uuid.UUID, platform: Platform) -> uuid.UUID:
    return uuid.uuid5(command_id, f"daily-automatic-collection:{platform.value}")


def _automatic_replay(
    *,
    command_id: uuid.UUID,
    batch_key: uuid.UUID,
    product: Product,
) -> AutomaticCollectionResult | None:
    operation_ids = [_automatic_operation_id(command_id, platform) for platform in PLATFORMS]
    runs = tuple(
        CollectionRun.objects.filter(operation_key__in=operation_ids)
        .select_related("source")
        .order_by("source__platform_code")
    )
    if not runs:
        return None
    if len(runs) != len(PLATFORMS):
        raise ValidationError(
            "This automatic collection command has an incomplete durable result; do not retry with a new command."
        )
    expected_platforms = {platform.value for platform in PLATFORMS}
    if (
        {run.source.platform_code for run in runs} != expected_platforms
        or any(run.batch_key != batch_key for run in runs)
        or any(str(run.query_spec.get("product_id", "")) != str(product.pk) for run in runs)
        or any(str(run.query_spec.get("automatic_command_id", "")) != str(command_id) for run in runs)
    ):
        raise ValidationError("This automatic collection command was already used for another payload.")
    evidence = tuple(
        ExternalEvidenceItem.objects.filter(collection_run_id__in=[run.pk for run in runs])
        .select_related("source", "collection_run")
        .order_by("observed_at", "id")
    )
    return AutomaticCollectionResult(
        command_id=command_id,
        runs=runs,
        evidence=evidence,
        created_count=0,
        created=False,
    )


def _connector_request_map(
    *,
    command_id: uuid.UUID,
    batch_key: uuid.UUID,
    product: Product,
    runs: tuple[CollectionRun, ...],
) -> dict[Platform, ConnectorRequest]:
    query_run = next((run for run in runs if run.query_spec.get("query")), runs[0])
    spec = query_run.query_spec
    try:
        window_start = datetime.fromisoformat(str(spec["window_start"]))
        window_end = datetime.fromisoformat(str(spec["window_end"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("Daily batch has an invalid immutable collection window.") from exc
    return {
        platform: ConnectorRequest(
            platform=platform,
            operation_key=f"daily-auto:{command_id.hex}:{platform.value.lower()}",
            query=str(spec.get("query", "")),
            window_start=window_start,
            window_end=window_end,
            market_code=product.market_code,
            language_code=product.language_code,
            max_items=50,
            metadata={
                "batch_key": str(batch_key),
                "product_id": str(product.pk),
                "collected_by": "daily-operations-runtime",
            },
        )
        for platform in PLATFORMS
    }


def _collection_state(result: ConnectorResult) -> tuple[str, str]:
    return {
        ConnectorRunStatus.SUCCEEDED: (CollectionRun.Status.SUCCEEDED, AvailabilityState.PRESENT),
        ConnectorRunStatus.PARTIAL: (CollectionRun.Status.PARTIAL, AvailabilityState.PRESENT),
        ConnectorRunStatus.MISSING: (CollectionRun.Status.BLOCKED, AvailabilityState.MISSING),
        ConnectorRunStatus.BLOCKED: (CollectionRun.Status.BLOCKED, AvailabilityState.BLOCKED),
        ConnectorRunStatus.UNAVAILABLE: (CollectionRun.Status.BLOCKED, AvailabilityState.UNAVAILABLE),
        ConnectorRunStatus.FAILED: (CollectionRun.Status.FAILED, AvailabilityState.UNAVAILABLE),
    }[result.status]


def _normalize_connector_items(
    *,
    result: ConnectorResult,
    request: ConnectorRequest,
    source: SourceRegistry,
    principal: Principal,
    observed_at,
):
    if result.mode not in {AcquisitionMode.API, AcquisitionMode.BROWSER}:
        raise ValidationError("Automatic results may be persisted only from an exact API or browser route.")
    return tuple(
        validate_connector_evidence(
            platform=result.platform,
            source_key=source.source_key,
            collection_run_key=request.operation_key,
            collected_by=str(principal.pk),
            collected_at=observed_at,
            acquisition_mode=result.mode,
            item=item,
            language_code=request.language_code,
            market_code=request.market_code,
            query=request.query,
        )
        for item in result.items
    )


def run_automatic_collection(
    *,
    batch_key: uuid.UUID,
    product: Product,
    command_id: uuid.UUID,
    principal: Principal,
    acting_role: str,
    runtime: DailyOperationsRuntime | None = None,
) -> AutomaticCollectionResult:
    """Try all seven configured API/browser routes and append exact terminal attempts.

    The default runtime has no network transport and no paired browser worker,
    therefore this call records explicit BLOCKED/MISSING/UNAVAILABLE outcomes
    and leaves the CSV/manual fallback visible.  A deployment must inject a
    reviewed runtime to perform external reads.
    """

    # Authorization must be resolved before constructing or invoking any
    # connector.  Otherwise an EDIT-only caller could trigger a browser/API
    # request (and potentially a paid provider) before persistence rejects it.
    for platform in PLATFORMS:
        require_authorization(
            principal=principal,
            acting_role=acting_role,
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            scope_kind=PermissionGrant.ScopeKind.PLATFORM,
            platform_code=platform.value,
        )
    initial_runs = batch_runs(batch_key=batch_key, product=product)
    replay = _automatic_replay(
        command_id=command_id,
        batch_key=batch_key,
        product=product,
    )
    if replay is not None:
        return replay
    requests = _connector_request_map(
        command_id=command_id,
        batch_key=batch_key,
        product=product,
        runs=initial_runs,
    )
    runtime = runtime or build_daily_operations_runtime()
    connector_batch = runtime.connectors.run(requests)
    started_at = timezone.now()
    completed_at = timezone.now()
    persisted_runs: list[CollectionRun] = []
    persisted_evidence: list[ExternalEvidenceItem] = []
    created_count = 0

    with transaction.atomic():
        # PostgreSQL serializes two browser/API button submissions for the same
        # immutable batch before the second replay check.
        tuple(CollectionRun.objects.select_for_update().filter(batch_key=batch_key).values_list("pk", flat=True))
        replay = _automatic_replay(
            command_id=command_id,
            batch_key=batch_key,
            product=product,
        )
        if replay is not None:
            return replay
        for platform in PLATFORMS:
            request = requests[platform]
            result = connector_batch.results[platform]
            if result.platform is not platform or result.operation_key != request.operation_key:
                raise ValidationError("Connector result does not bind the exact platform operation.")
            source_mode = result.mode if result.mode in {AcquisitionMode.API, AcquisitionMode.BROWSER} else PRIMARY_MODE[platform]
            source = _latest_source(platform, source_mode)
            normalized_items = ()
            if result.status in {ConnectorRunStatus.SUCCEEDED, ConnectorRunStatus.PARTIAL}:
                if not result.items:
                    raise ValidationError("A successful or partial connector result must contain evidence items.")
                normalized_items = _normalize_connector_items(
                    result=result,
                    request=request,
                    source=source,
                    principal=principal,
                    observed_at=completed_at,
                )
            run_status, availability = _collection_state(result)
            query_spec = dict(initial_runs[0].query_spec)
            query_spec.update(
                {
                    "automatic_command_id": str(command_id),
                    "connector_operation_key": request.operation_key,
                    "connector_status": result.status.value,
                    "connector_mode": result.mode.value if result.mode else "",
                    "connector_provider": result.provider or "",
                }
            )
            outcome = record_collection_run(
                source=source,
                batch_key=batch_key,
                attempt_number=_next_attempt(batch_key=batch_key, source=source),
                operation_key=_automatic_operation_id(command_id, platform),
                query_spec=query_spec,
                status=run_status,
                availability_state=availability,
                started_at=started_at,
                completed_at=completed_at,
                result_summary={
                    "connector_status": result.status.value,
                    "mode": result.mode.value if result.mode else "",
                    "provider": result.provider or "",
                    "items": len(normalized_items),
                    "reason": result.reason[:2_000],
                    "retryable": result.retryable,
                    "provenance": [dict(value) for value in result.provenance],
                    "fallback": "CSV_OR_MANUAL" if not normalized_items else "",
                },
                error_code="" if normalized_items else f"CONNECTOR_{result.status.value}",
                principal=principal,
                acting_role=acting_role,
            )
            persisted_runs.append(outcome.run)
            for item in normalized_items:
                evidence, created = _persist_ingested_evidence(
                    run=outcome.run,
                    source=source,
                    item=item,
                    principal=principal,
                )
                persisted_evidence.append(evidence)
                created_count += int(created)
    return AutomaticCollectionResult(
        command_id=command_id,
        runs=tuple(persisted_runs),
        evidence=tuple(persisted_evidence),
        created_count=created_count,
        created=True,
    )


def _platform_collection_replay(
    *,
    command_id: uuid.UUID,
    batch_key: uuid.UUID,
    product: Product,
    platform: Platform,
) -> PlatformCollectionResult | None:
    run = (
        CollectionRun.objects.filter(operation_key=_automatic_operation_id(command_id, platform))
        .select_related("source")
        .first()
    )
    if run is None:
        return None
    if (
        run.batch_key != batch_key
        or run.source.platform_code != platform.value
        or str(run.query_spec.get("product_id", "")) != str(product.pk)
        or str(run.query_spec.get("automatic_command_id", "")) != str(command_id)
    ):
        raise ValidationError("This platform collection command was already used for another payload.")
    evidence = tuple(
        ExternalEvidenceItem.objects.filter(collection_run=run)
        .select_related("source", "collection_run")
        .order_by("observed_at", "id")
    )
    return PlatformCollectionResult(
        command_id=command_id,
        platform=platform,
        run=run,
        evidence=evidence,
        created_count=0,
        created=False,
    )


def run_platform_collection(
    *,
    batch_key: uuid.UUID,
    product: Product,
    command_id: uuid.UUID,
    platform: Platform,
    principal: Principal,
    acting_role: str,
    runtime: DailyOperationsRuntime | None = None,
) -> PlatformCollectionResult:
    """Run and persist one platform so the UI can show honest progress."""

    # Keep the external side effect behind the exact live collection grant.
    # This is deliberately before replay lookup and runtime construction so a
    # revoked/denied caller cannot use an old command or trigger a connector.
    require_authorization(
        principal=principal,
        acting_role=acting_role,
        action=PermissionGrant.Action.COLLECT_READ_ONLY,
        scope_kind=PermissionGrant.ScopeKind.PLATFORM,
        platform_code=platform.value,
    )
    initial_runs = batch_runs(batch_key=batch_key, product=product)
    replay = _platform_collection_replay(
        command_id=command_id,
        batch_key=batch_key,
        product=product,
        platform=platform,
    )
    if replay is not None:
        return replay
    requests = _connector_request_map(
        command_id=command_id,
        batch_key=batch_key,
        product=product,
        runs=initial_runs,
    )
    runtime = runtime or build_daily_operations_runtime()
    result = runtime.connectors.run_one(requests[platform])
    request = requests[platform]
    if result.platform is not platform or result.operation_key != request.operation_key:
        raise ValidationError("Connector result does not bind the exact platform operation.")
    started_at = timezone.now()
    completed_at = timezone.now()

    with transaction.atomic():
        tuple(
            CollectionRun.objects.select_for_update()
            .filter(batch_key=batch_key)
            .values_list("pk", flat=True)
        )
        replay = _platform_collection_replay(
            command_id=command_id,
            batch_key=batch_key,
            product=product,
            platform=platform,
        )
        if replay is not None:
            return replay
        source_mode = (
            result.mode
            if result.mode in {AcquisitionMode.API, AcquisitionMode.BROWSER}
            else PRIMARY_MODE[platform]
        )
        source = _latest_source(platform, source_mode)
        normalized_items = ()
        if result.status in {ConnectorRunStatus.SUCCEEDED, ConnectorRunStatus.PARTIAL}:
            if not result.items:
                raise ValidationError("A successful or partial connector result must contain evidence items.")
            normalized_items = _normalize_connector_items(
                result=result,
                request=request,
                source=source,
                principal=principal,
                observed_at=completed_at,
            )
        run_status, availability = _collection_state(result)
        query_spec = dict(initial_runs[0].query_spec)
        query_spec.update(
            {
                "automatic_command_id": str(command_id),
                "connector_operation_key": request.operation_key,
                "connector_status": result.status.value,
                "connector_mode": result.mode.value if result.mode else "",
                "connector_provider": result.provider or "",
            }
        )
        outcome = record_collection_run(
            source=source,
            batch_key=batch_key,
            attempt_number=_next_attempt(batch_key=batch_key, source=source),
            operation_key=_automatic_operation_id(command_id, platform),
            query_spec=query_spec,
            status=run_status,
            availability_state=availability,
            started_at=started_at,
            completed_at=completed_at,
            result_summary={
                "connector_status": result.status.value,
                "mode": result.mode.value if result.mode else "",
                "provider": result.provider or "",
                "items": len(normalized_items),
                "reason": result.reason[:2_000],
                "retryable": result.retryable,
                "provenance": [dict(value) for value in result.provenance],
                "fallback": "CSV_OR_MANUAL" if not normalized_items else "",
            },
            error_code="" if normalized_items else f"CONNECTOR_{result.status.value}",
            principal=principal,
            acting_role=acting_role,
        )
        evidence: list[ExternalEvidenceItem] = []
        created_count = 0
        for item in normalized_items:
            persisted, created = _persist_ingested_evidence(
                run=outcome.run,
                source=source,
                item=item,
                principal=principal,
            )
            evidence.append(persisted)
            created_count += int(created)
    return PlatformCollectionResult(
        command_id=command_id,
        platform=platform,
        run=outcome.run,
        evidence=tuple(evidence),
        created_count=created_count,
        created=True,
    )


def _batch_evidence(*, batch_key: uuid.UUID, product: Product) -> tuple[ExternalEvidenceItem, ...]:
    runs = batch_runs(batch_key=batch_key, product=product)
    run_ids = [run.pk for run in runs]
    return tuple(
        ExternalEvidenceItem.objects.filter(
            collection_run_id__in=run_ids,
            invalidation_event__isnull=True,
        )
        .select_related("source", "collection_run")
        # UUIDv7 creation order keeps the first item a stable version anchor
        # even when a later CSV contains an older observed_at timestamp.
        .order_by("id")
    )


def _analysis_request(*, batch_key: uuid.UUID, product: Product, evidence) -> AIRequest:
    if len(evidence) > MAX_ANALYSIS_EVIDENCE:
        raise ValidationError(
            f"Daily analysis accepts at most {MAX_ANALYSIS_EVIDENCE} exact evidence items; narrow the batch first."
        )
    evidence_ids = [str(item.pk) for item in evidence]
    evidence_fingerprint = canonical_sha256(evidence_ids)
    compact_evidence = [
        {
            "platform": item.platform_code,
            "title": item.title,
            "excerpt": item.excerpt[:2000],
            "url": item.external_url,
            "external_id": item.external_content_id,
            "observed_at": item.observed_at.isoformat(),
        }
        for item in evidence
    ]
    return AIRequest(
        messages=(
            AIMessage(
                role="system",
                content=(
                    "You propose Daily Operations market analysis. Use only supplied external-demand evidence. "
                    "Do not treat internal performance, commerce, process, QuickTest, or GEO as demand. "
                    "Return a proposal for human review; never claim approval or activate work."
                ),
            ),
            AIMessage(
                role="user",
                content=(
                    f"Product: {product.name} ({product.market_code}/{product.language_code})\n"
                    f"Batch: {batch_key}\nEvidence: {compact_evidence}"
                ),
            ),
        ),
        output=StructuredOutputSpec(name="daily_operations_analysis", schema=DAILY_ANALYSIS_SCHEMA),
        operation_key=f"daily-analysis:{batch_key.hex}:{evidence_fingerprint}",
        max_output_tokens=2400,
        temperature=0.0,
        metadata={
            "product_id": str(product.pk),
            "batch_key": str(batch_key),
            "evidence_ids": evidence_ids,
            "evidence_fingerprint": evidence_fingerprint,
        },
    )


@transaction.atomic
def propose_daily_analysis(
    *,
    batch_key: uuid.UUID,
    product: Product,
    principal: Principal,
    acting_role: str,
    provider: AIProvider | None = None,
) -> SignalAssessment:
    """Create an immutable AI/dry-run proposal; never an approved business decision."""

    require_authorization(
        principal=principal,
        acting_role=acting_role,
        action=PermissionGrant.Action.EDIT,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=product,
    )
    evidence = _batch_evidence(batch_key=batch_key, product=product)
    if not evidence:
        raise ValidationError("At least one provenance-linked evidence item is required before analysis.")
    if len(evidence) > MAX_ANALYSIS_EVIDENCE:
        raise ValidationError(
            f"Daily analysis accepts at most {MAX_ANALYSIS_EVIDENCE} exact evidence items; narrow the batch first."
        )
    assessment_key = f"daily-analysis-{batch_key.hex}"
    evidence_ids = [str(item.pk) for item in evidence]
    evidence_fingerprint = canonical_sha256(evidence_ids)
    latest = SignalAssessment.objects.filter(assessment_key=assessment_key).order_by("-version_number").first()
    if latest:
        latest_ids = list(latest.value.get("evidence_ids", []))
        latest_fingerprint = latest.value.get("evidence_fingerprint") or canonical_sha256(latest_ids)
        if latest_ids == evidence_ids and latest_fingerprint == evidence_fingerprint:
            if latest.method == AssessmentMethod.AI_PROPOSAL:
                return latest
            if latest.supersedes_id and latest.supersedes.method == AssessmentMethod.AI_PROPOSAL:
                return latest.supersedes
        if latest.method != AssessmentMethod.AI_PROPOSAL or latest.decision_state != DecisionState.PROPOSED:
            raise ValidationError(
                "This evidence set changed after a human decision; start a new Daily batch instead of rewriting it."
            )
    query = next(
        (
            str(run.query_spec.get("query", ""))
            for run in batch_runs(batch_key=batch_key, product=product)
            if run.query_spec.get("query")
        ),
        "",
    )
    # The composition root is fail-closed: without an explicit reviewed live
    # configuration this is a deterministic Dry Run and is never labelled as
    # a DeepSeek response.  Tests or deployment composition may inject the
    # approved DeepSeek V4 provider explicitly.
    provider = provider or build_daily_operations_runtime(
        DailyOperationsRuntimeConfig(
            dry_run_output=deterministic_analysis(
                query=query,
                evidence_count=len(evidence),
                first_title=evidence[0].title,
            )
        )
    ).ai_provider
    request = _analysis_request(batch_key=batch_key, product=product, evidence=evidence)
    result = provider.generate(request)
    output = dict(result.output)
    output.update(
        {
            "batch_key": str(batch_key),
            "product_id": str(product.pk),
            "evidence_ids": evidence_ids,
            "evidence_fingerprint": evidence_fingerprint,
            "ai_request_fingerprint": result.request_fingerprint,
            "ai_status": result.status.value,
        }
    )
    return SignalAssessment.objects.create(
        evidence_item=evidence[0],
        assessment_key=assessment_key,
        version_number=(latest.version_number + 1) if latest else 1,
        signal_type="DAILY_OPERATIONS_ANALYSIS",
        value=output,
        confidence=Decimal(str(result.output["confidence"])),
        method=AssessmentMethod.AI_PROPOSAL,
        decision_state=DecisionState.PROPOSED,
        rationale="AI output is retained only as a proposal pending an explicit human decision.",
        model_reference=f"{result.provider}:{result.model}",
        supersedes=latest,
        assessed_by_principal=principal,
    )


def _decimal_score(value, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise ValidationError({field_name: "Score must be a number between 0 and 1."}) from error
    if result < 0 or result > 1:
        raise ValidationError({field_name: "Score must be between 0 and 1."})
    return result.quantize(Decimal("0.0001"))


@transaction.atomic
def accept_daily_analysis(
    *,
    proposal: SignalAssessment,
    product: Product,
    principal: Principal,
    acting_role: str,
) -> ProductOpportunity:
    """Turn an AI proposal into separate human-authored approved facts."""

    grant = require_authorization(
        principal=principal,
        acting_role=acting_role,
        action=PermissionGrant.Action.EDIT,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=product,
    )
    if (
        proposal.method != AssessmentMethod.AI_PROPOSAL
        or proposal.decision_state != DecisionState.PROPOSED
        or proposal.signal_type != "DAILY_OPERATIONS_ANALYSIS"
    ):
        raise ValidationError("Only a pending Daily Operations AI proposal can be accepted.")
    value = dict(proposal.value)
    if value.get("product_id") != str(product.pk):
        raise ValidationError("Analysis proposal belongs to another Product.")
    batch_key = uuid.UUID(str(value.get("batch_key")))
    evidence = _batch_evidence(batch_key=batch_key, product=product)
    exact_ids = [str(item.pk) for item in evidence]
    if exact_ids != value.get("evidence_ids"):
        raise ValidationError("The proposal evidence set changed; generate a new proposal.")
    decision = SignalAssessment.objects.filter(supersedes=proposal).first()
    if decision:
        opportunity = ProductOpportunity.objects.filter(
            opportunity_key=f"daily-{batch_key.hex}"
        ).first()
        if opportunity is None:
            raise ValidationError("Analysis decision exists without its immutable Opportunity.")
        return opportunity

    SignalAssessment.objects.create(
        evidence_item=proposal.evidence_item,
        assessment_key=proposal.assessment_key,
        version_number=proposal.version_number + 1,
        signal_type=proposal.signal_type,
        value=value,
        confidence=_decimal_score(value["confidence"], "confidence"),
        method=AssessmentMethod.HUMAN,
        decision_state=DecisionState.APPROVED,
        rationale="A human accepted this exact AI proposal and evidence set.",
        supersedes=proposal,
        assessed_by_principal=principal,
        decided_by_principal=principal,
    )
    topic = Topic.objects.create(
        topic_key=f"daily-{batch_key.hex}",
        version_number=1,
        market_code=product.market_code,
        language_code=product.language_code,
        label=str(value["topic_label"])[:240],
        summary=str(value["summary"]),
        search_intent=str(value.get("search_intent", ""))[:80],
        pain_points=list(value.get("pain_points", [])),
        job_to_be_done=str(value.get("job_to_be_done", "")),
        decision_state=DecisionState.APPROVED,
        created_by_principal=principal,
    )
    for item in evidence:
        TopicEvidenceLink.objects.create(
            topic=topic,
            evidence_item=item,
            linkage_role=TopicEvidenceLink.Role.PRIMARY,
            created_by_principal=principal,
        )
    runs = batch_runs(batch_key=batch_key, product=product)
    demand = DemandAssessment.objects.create(
        topic=topic,
        version_number=1,
        window_start=min(run.started_at for run in runs),
        window_end=max(run.completed_at for run in runs) + timedelta(microseconds=1),
        demand_score=_decimal_score(value["demand_score"], "demand_score"),
        velocity_score=_decimal_score(value["velocity_score"], "velocity_score"),
        confidence=_decimal_score(value["confidence"], "confidence"),
        availability_state=AvailabilityState.PRESENT,
        method=AssessmentMethod.HUMAN,
        decision_state=DecisionState.APPROVED,
        rationale="Human-approved assessment of the exact external evidence set.",
        assessed_by_principal=principal,
    )
    weight = (Decimal("1") / Decimal(len(evidence))).quantize(Decimal("0.0001"))
    for item in evidence:
        DemandEvidenceLink.objects.create(
            demand_assessment=demand,
            evidence_item=item,
            weight=weight,
            created_by_principal=principal,
        )
    fit = ProductTopicFit.objects.create(
        product=product,
        topic=topic,
        created_by_principal=principal,
    )
    fit_assessment = ProductTopicFitAssessment.objects.create(
        product_topic_fit=fit,
        version_number=1,
        fit_score=_decimal_score(value["fit_score"], "fit_score"),
        evidence_strength=_decimal_score(value["evidence_strength"], "evidence_strength"),
        method=AssessmentMethod.HUMAN,
        decision_state=DecisionState.APPROVED,
        rationale="Human accepted product fit using the sealed Product context and external evidence.",
        assessed_by_principal=principal,
    )
    opportunity_payload = {
        "batch_key": str(batch_key),
        "product_id": str(product.pk),
        "topic_id": str(topic.pk),
        "demand_assessment_id": str(demand.pk),
        "fit_assessment_id": str(fit_assessment.pk),
        "recommendation": value["recommendation"],
    }
    return ProductOpportunity.objects.create(
        product=product,
        topic=topic,
        demand_assessment=demand,
        product_topic_fit_assessment=fit_assessment,
        opportunity_key=f"daily-{batch_key.hex}",
        title=str(value["topic_label"])[:240],
        recommendation=str(value["recommendation"]),
        priority_score=_decimal_score(value["priority_score"], "priority_score"),
        risk_level=str(value.get("risk_level", RiskLevel.MEDIUM)),
        creation_command_id=uuid.uuid5(batch_key, "create-opportunity"),
        creation_payload_hash=canonical_sha256(opportunity_payload),
        created_by_principal=principal,
        created_under_grant=grant,
        updated_by_principal=principal,
    )


@transaction.atomic
def create_initiative_from_opportunity(
    *,
    opportunity: ProductOpportunity,
    command_id: uuid.UUID,
    principal: Principal,
    acting_role: str,
) -> Initiative:
    if opportunity.current_state != ProductOpportunity.State.APPROVED:
        raise ValidationError("Only a human-approved Opportunity can become an Initiative.")
    existing = Initiative.objects.filter(creation_command_id=command_id).first()
    payload = {
        "opportunity_id": str(opportunity.pk),
        "product_id": str(opportunity.product_id),
        "title": opportunity.title,
        "objective": opportunity.recommendation,
    }
    payload_hash = canonical_sha256(payload)
    if existing:
        if existing.creation_payload_hash != payload_hash:
            raise ValidationError("Initiative command was replayed with different input.")
        return existing
    grant = require_authorization(
        principal=principal,
        acting_role=acting_role,
        action=PermissionGrant.Action.EDIT,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=opportunity.product,
    )
    return Initiative.objects.create(
        product=opportunity.product,
        opportunity=opportunity,
        initiative_key=f"initiative-{opportunity.opportunity_key}",
        title=opportunity.title,
        objective=opportunity.recommendation,
        creation_command_id=command_id,
        creation_payload_hash=payload_hash,
        created_by_principal=principal,
        created_under_grant=grant,
        updated_by_principal=principal,
    )


@transaction.atomic
def create_channel_plan(
    *,
    initiative: Initiative,
    platform: Platform,
    command_id: uuid.UUID,
    plan_date,
    goal: dict,
    content_requirements: dict,
    principal: Principal,
    acting_role: str,
    channel_account=None,
) -> ChannelPlan:
    if initiative.current_state not in {Initiative.State.APPROVED, Initiative.State.ACTIVE}:
        raise ValidationError("Channel planning requires an approved Initiative.")
    # Runtime environment and capability are system-owned facts.  Discard the
    # legacy form keys so callers cannot choose or spoof deployment context.
    requested_requirements = dict(content_requirements or {})
    for system_owned_key in (
        "environment_code",
        "capability_code",
        "runtime_binding_id",
        "runtime_environment_code",
        "capability_state_id",
        "resolved_capability_code",
    ):
        requested_requirements.pop(system_owned_key, None)
    existing = ChannelPlan.objects.filter(creation_command_id=command_id).first()
    payload = {
        "initiative_id": str(initiative.pk),
        "platform": platform.value,
        "channel_account_id": str(getattr(channel_account, "pk", "")),
        "plan_date": plan_date.isoformat(),
        "goal": goal,
        "content_requirements": requested_requirements,
    }
    payload_hash = canonical_sha256(payload)
    if existing:
        if existing.creation_payload_hash != payload_hash:
            raise ValidationError("ChannelPlan command was replayed with different input.")
        return existing
    if channel_account is None:
        raise ValidationError("请先选择一个与执行平台匹配的账号。")
    if channel_account.platform_code != platform.value:
        raise ValidationError("所选账号与执行平台不匹配。")
    if channel_account.status != channel_account.Status.ACTIVE:
        raise ValidationError("所选账号当前不可用。")
    require_authorization(
        principal=principal,
        acting_role=acting_role,
        action=PermissionGrant.Action.COLLECT_READ_ONLY,
        scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
        product=initiative.product,
        platform_code=channel_account.platform_code,
        account_ref=channel_account.account_code,
    )
    binding, capability = _resolve_current_plan_runtime(channel_account)
    grant = require_authorization(
        principal=principal,
        acting_role=acting_role,
        action=PermissionGrant.Action.EDIT,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=initiative.product,
    )
    resolved_requirements = {
        **requested_requirements,
        "runtime_binding_id": str(binding.pk),
        "runtime_environment_code": binding.runtime_environment.environment_code,
        "capability_state_id": str(capability.pk),
        "resolved_capability_code": capability.capability_code,
    }
    return ChannelPlan.objects.create(
        initiative=initiative,
        channel_account=channel_account,
        plan_key=f"{initiative.initiative_key}-{platform.value.lower()}",
        platform_code=platform.value,
        plan_date=plan_date,
        goal=goal,
        content_requirements=resolved_requirements,
        creation_command_id=command_id,
        creation_payload_hash=payload_hash,
        created_by_principal=principal,
        created_under_grant=grant,
        updated_by_principal=principal,
    )


def _resolve_current_plan_runtime(
    channel_account,
    *,
    at=None,
) -> tuple[AccountEnvironmentBinding, CapabilityState]:
    """Resolve one exact current binding and its manual-publish capability.

    There is deliberately no ``.first()`` fallback: zero matches are an
    incomplete setup and multiple matches are ambiguous, so both fail closed.
    """

    at = at or timezone.now()
    bindings = (
        AccountEnvironmentBinding.objects.select_related("runtime_environment")
        .filter(
            channel_account_id=channel_account.pk,
            status=AccountEnvironmentBinding.Status.ACTIVE,
            valid_from__lte=at,
            runtime_environment__status="ACTIVE",
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=at))
        .order_by("runtime_environment__environment_code", "-binding_version", "id")
    )
    current_bindings = [binding for binding in bindings if binding.is_current_at(at)]
    if not current_bindings:
        raise ValidationError("这个账号当前没有可用的运行环境，请先完成账号与环境配置。")
    if len(current_bindings) > 1:
        raise ValidationError("这个账号同时连接了多个运行环境，系统不能安全猜选；请先只保留一个当前绑定。")
    binding = current_bindings[0]
    capability = (
        CapabilityState.objects.filter(
            account_environment_binding=binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
        )
        .order_by("-state_version")
        .first()
    )
    if capability is None:
        raise ValidationError("这个账号当前没有人工发布能力配置，请先完成运行配置。")
    if not capability.is_current_open_at(at):
        raise ValidationError("这个账号当前不能人工发布，请先检查账号能力状态。")
    return binding, capability


def _current_capability_for_plan(plan: ChannelPlan) -> CapabilityState:
    if not plan.channel_account_id:
        raise ValidationError("Choose the exact ChannelAccount before compiling this plan.")
    # Older plans may contain user-entered environment_code/capability_code
    # values.  They remain untouched for audit, but are never trusted during
    # compilation; the exact current binding is resolved fail-closed instead.
    _, capability = _resolve_current_plan_runtime(plan.channel_account)
    return capability


def _latest_contract(profile) -> TaskContractVersion:
    contract = (
        TaskContractVersion.objects.filter(product_profile_version=profile)
        .order_by("-version_number", "-created_at", "-id")
        .first()
    )
    if contract is None or not contract.sealed_at:
        raise ValidationError("The current Product Profile has no sealed Task Contract.")
    return contract


def _policy_snapshot(contract: TaskContractVersion) -> list[dict[str, str]]:
    policies = [
        link.policy_version
        for link in contract.policy_links.select_related("policy_version").filter(required=True)
    ]
    if not policies:
        raise ValidationError("Task Contract has no exact required Policy set.")
    return [
        {"id": str(policy.pk), "manifest_sha256": policy.manifest_sha256}
        for policy in sorted(policies, key=lambda item: str(item.pk))
    ]


@transaction.atomic
def compile_channel_plan_task(
    *,
    channel_plan: ChannelPlan,
    task_id: uuid.UUID,
    command_id: uuid.UUID,
    principal: Principal,
    acting_role: str,
) -> CompiledTaskResult:
    """Compile one real workflow Task from exact sealed planning context."""

    existing_context = TaskCompilationContext.objects.filter(compilation_command_id=command_id).first()
    if existing_context:
        return CompiledTaskResult(existing_context.task, existing_context, False)
    plan = ChannelPlan.objects.select_for_update().select_related(
        "initiative__product", "channel_account"
    ).get(pk=channel_plan.pk)
    if plan.current_state not in {ChannelPlan.State.READY, ChannelPlan.State.ACTIVE}:
        raise ValidationError("ChannelPlan must be READY before Task Compiler can run.")
    product = plan.initiative.product
    profile = product.current_profile_version
    if profile is None or not profile.is_sealed:
        raise ValidationError("Task Compiler requires the current sealed Product Profile.")
    if not all(
        (
            profile.objective_profile_version_id,
            profile.claim_matrix_version_id,
            profile.evidence_library_version_id,
        )
    ):
        raise ValidationError("Product Profile is missing exact Objective, Claim, or Evidence versions.")
    contract = _latest_contract(profile)
    capability = _current_capability_for_plan(plan)
    policy_snapshot = _policy_snapshot(contract)
    task_title = str(plan.content_requirements.get("task_title") or plan.goal.get("title") or plan.platform_code)
    task_title = f"{plan.plan_date.isoformat()} · {task_title}"[:240]
    task_description = str(
        plan.content_requirements.get("task_description")
        or f"执行 {plan.platform_code} ChannelPlan：{plan.initiative.objective}"
    )
    task, created = Task.create_draft(
        task_id=task_id,
        command_id=uuid.uuid5(command_id, "workflow-task"),
        product_profile_version_id=profile.pk,
        contract_version_id=contract.pk,
        title=task_title,
        description=task_description,
        actor_principal=principal,
        acting_role=acting_role,
    )
    compilation_payload = {
        "task_id": str(task.pk),
        "channel_plan_id": str(plan.pk),
        "profile_id": str(profile.pk),
        "contract_id": str(contract.pk),
        "objective_profile_version_id": str(profile.objective_profile_version_id),
        "claim_matrix_version_id": str(profile.claim_matrix_version_id),
        "evidence_library_version_id": str(profile.evidence_library_version_id),
        "policy_snapshot": policy_snapshot,
        "capability_state_id": str(capability.pk),
        "compiler_version": "daily-operations-v1",
    }
    input_hash = canonical_sha256(compilation_payload)
    context = TaskCompilationContext.objects.create(
        task=task,
        channel_plan=plan,
        product=product,
        product_profile_version=profile,
        task_contract_version=contract,
        objective_profile_version=profile.objective_profile_version,
        objective_profile_manifest_sha256=profile.objective_profile_version.manifest_sha256,
        claim_matrix_version=profile.claim_matrix_version,
        claim_matrix_manifest_sha256=profile.claim_matrix_version.manifest_sha256,
        evidence_library_version=profile.evidence_library_version,
        evidence_library_manifest_sha256=profile.evidence_library_version.manifest_sha256,
        policy_set_snapshot=policy_snapshot,
        policy_set_sha256="",
        capability_state=capability,
        compiler_name="growth-os-daily-task-compiler",
        compiler_version="1.0.0",
        input_payload_sha256=input_hash,
        compilation_command_id=command_id,
        compiled_by_principal=principal,
        permission_grant=task.created_under_grant,
    )
    return CompiledTaskResult(task, context, created)


def _flow_command(command_id: uuid.UUID, step: str) -> uuid.UUID:
    """Derive a stable command for each immutable step of one UI action."""

    return uuid.uuid5(command_id, step)


@transaction.atomic
def accept_analysis_and_start_execution_project(
    *,
    proposal: SignalAssessment,
    product: Product,
    command_id: uuid.UUID,
    principal: Principal,
    acting_role: str,
) -> StartedExecutionProjectResult:
    """Compress Owner planning clicks without compressing audit facts.

    The shortcut deliberately stops at an ACTIVE Initiative.  Operator
    submission, Admin review, release gating and publication confirmation are
    not part of this transaction and remain independently authorized actions.
    """

    # Serialize two browser tabs accepting the same immutable proposal.  The
    # nested services still perform their own exact authorization checks.
    locked_proposal = SignalAssessment.objects.select_for_update().get(pk=proposal.pk)
    opportunity = accept_daily_analysis(
        proposal=locked_proposal,
        product=product,
        principal=principal,
        acting_role=acting_role,
    )

    if opportunity.current_state == ProductOpportunity.State.PROPOSED:
        opportunity = transition_opportunity(
            opportunity_id=opportunity.pk,
            to_state=ProductOpportunity.State.TRIAGED,
            expected_version=opportunity.state_version,
            command_id=_flow_command(command_id, "owner-opportunity-triaged"),
            reason="Owner accepted the exact evidence-backed suggestion for planning.",
            principal=principal,
            acting_role=acting_role,
        ).aggregate
    if opportunity.current_state == ProductOpportunity.State.TRIAGED:
        opportunity = transition_opportunity(
            opportunity_id=opportunity.pk,
            to_state=ProductOpportunity.State.APPROVED,
            expected_version=opportunity.state_version,
            command_id=_flow_command(command_id, "owner-opportunity-approved"),
            reason="Owner confirmed this direction should enter execution.",
            principal=principal,
            acting_role=acting_role,
        ).aggregate
    if opportunity.current_state != ProductOpportunity.State.APPROVED:
        raise ValidationError("This suggestion is no longer eligible to start an execution project.")

    initiative_command = _flow_command(command_id, "owner-initiative-created")
    anchored_initiative = Initiative.objects.filter(creation_command_id=initiative_command).first()
    initiative = Initiative.objects.select_for_update().filter(opportunity=opportunity).first()
    if anchored_initiative is not None and (
        initiative is None or anchored_initiative.pk != initiative.pk
    ):
        raise CommandReplayConflict(
            "The command ID was already used to start another execution project."
        )
    if initiative is None:
        initiative = create_initiative_from_opportunity(
            opportunity=opportunity,
            command_id=initiative_command,
            principal=principal,
            acting_role=acting_role,
        )
    elif (
        initiative.current_state == Initiative.State.ACTIVE
        and initiative.creation_command_id != initiative_command
        and not InitiativeStateEvent.objects.filter(
            initiative=initiative,
            command_id__in=(
                _flow_command(command_id, "owner-initiative-approved"),
                _flow_command(command_id, "owner-initiative-active"),
            ),
        ).exists()
    ):
        # There is nothing left for this new command to do, so do not pretend
        # that it created or approved an already-active historical project.
        raise ValidationError("This execution project is already active.")

    if initiative.current_state == Initiative.State.PROPOSED:
        initiative = transition_initiative(
            initiative_id=initiative.pk,
            to_state=Initiative.State.APPROVED,
            expected_version=initiative.state_version,
            command_id=_flow_command(command_id, "owner-initiative-approved"),
            reason="Owner approved the project created from the exact opportunity.",
            principal=principal,
            acting_role=acting_role,
        ).aggregate
    if initiative.current_state == Initiative.State.APPROVED:
        initiative = transition_initiative(
            initiative_id=initiative.pk,
            to_state=Initiative.State.ACTIVE,
            expected_version=initiative.state_version,
            command_id=_flow_command(command_id, "owner-initiative-active"),
            reason=(
                "Owner started planning work; submission, review, release gate "
                "and publication remain separate."
            ),
            principal=principal,
            acting_role=acting_role,
        ).aggregate
    if initiative.current_state != Initiative.State.ACTIVE:
        raise ValidationError("This execution project cannot be started from its current state.")
    return StartedExecutionProjectResult(opportunity=opportunity, initiative=initiative)


@transaction.atomic
def confirm_channel_plan_and_compile_task(
    *,
    channel_plan: ChannelPlan,
    task_id: uuid.UUID,
    command_id: uuid.UUID,
    principal: Principal,
    acting_role: str,
) -> ConfirmedPlanTaskResult:
    """Confirm one plan and compile one task while retaining every event."""

    plan = ChannelPlan.objects.select_for_update().select_related("initiative__product").get(
        pk=channel_plan.pk
    )
    require_authorization(
        principal=principal,
        acting_role=acting_role,
        action=PermissionGrant.Action.EDIT,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=plan.initiative.product,
    )
    compilation_command = _flow_command(command_id, "owner-channel-plan-compiled")
    replay = TaskCompilationContext.objects.filter(
        compilation_command_id=compilation_command
    ).select_related("task", "channel_plan").first()
    if replay is not None:
        if replay.channel_plan_id != plan.pk or replay.task_id != task_id:
            raise CommandReplayConflict(
                "The command ID was already used with another plan or task ID."
            )
        return ConfirmedPlanTaskResult(
            channel_plan=plan,
            compilation=CompiledTaskResult(replay.task, replay, False),
        )
    if plan.compilation_contexts.exists():
        raise ValidationError("This platform plan already has an execution task.")

    if plan.current_state == ChannelPlan.State.DRAFT:
        plan = transition_channel_plan(
            channel_plan_id=plan.pk,
            to_state=ChannelPlan.State.READY,
            expected_version=plan.state_version,
            command_id=_flow_command(command_id, "owner-channel-plan-ready"),
            reason="Owner confirmed the exact account, date and delivery requirements.",
            principal=principal,
            acting_role=acting_role,
        ).aggregate
    if plan.current_state == ChannelPlan.State.READY:
        plan = transition_channel_plan(
            channel_plan_id=plan.pk,
            to_state=ChannelPlan.State.ACTIVE,
            expected_version=plan.state_version,
            command_id=_flow_command(command_id, "owner-channel-plan-active"),
            reason="Owner activated this platform plan for task compilation.",
            principal=principal,
            acting_role=acting_role,
        ).aggregate
    if plan.current_state != ChannelPlan.State.ACTIVE:
        raise ValidationError("This platform plan cannot generate a task from its current state.")

    compilation = compile_channel_plan_task(
        channel_plan=plan,
        task_id=task_id,
        command_id=compilation_command,
        principal=principal,
        acting_role=acting_role,
    )
    return ConfirmedPlanTaskResult(channel_plan=plan, compilation=compilation)
