from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlparse

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.authorization import require_authorization
from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from insights.models import (
    AvailabilityState,
    ChannelPerformanceObservation,
    DataDomain,
    GEOMetricObservation,
    GEOProbePanel,
    GEOProbePanelItem,
    GEOProbeCitation,
    GEOProbeResult,
    GEOProbeRun,
    LearningEvidenceLink,
    LearningVersion,
    MetricCollectionRun,
    MetricCollectionRunMetric,
    MetricDefinition,
    PublicationPerformanceObservation,
)
from intelligence.models import ChannelPlan
from releasegate.models import Publication


GEO_PANEL_MANAGER_ROLES = {
    Principal.Role.OWNER,
    Principal.Role.OPERATIONS_ADMIN,
}
LOCAL_TEST_GEO_PANEL_KEY = "local-test-puko-geo"


def is_local_test_geo_item(item: GEOProbePanelItem) -> bool:
    return bool(
        item.panel.panel_key == LOCAL_TEST_GEO_PANEL_KEY
        or (item.intent or "").upper().startswith("LOCAL_")
    )


# Backward-compatible private name for the existing internal callers.
_is_local_test_geo_item = is_local_test_geo_item


def _sha256(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_active_account(account):
    if account.status != account.Status.ACTIVE:
        raise ValidationError({"channel_account": "只能为启用中的平台账号记录数据。"})


def _collection_grant(*, actor, account):
    _require_active_account(account)
    return require_authorization(
        principal=actor,
        acting_role=actor.role,
        action=PermissionGrant.Action.COLLECT_READ_ONLY,
        scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
        account_ref=account.account_code,
    )


def _product_grant(*, actor, product, action):
    return require_authorization(
        principal=actor,
        acting_role=actor.role,
        action=action,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=product,
    )


def _geo_panel_management_grant(*, actor, product):
    """Resolve the exact live grant used for one GEO configuration write.

    Role controls which staff level may enter this configuration workflow;
    the role itself never grants access.  Every write still resolves the
    actor's exact current product-scoped EDIT grant immediately before the
    append-only row is created.
    """

    if actor.role not in GEO_PANEL_MANAGER_ROLES:
        raise PermissionDenied("GEO_PANEL_CONFIGURATION_REQUIRES_OWNER_OR_ADMIN")
    return _product_grant(
        actor=actor,
        product=product,
        action=PermissionGrant.Action.EDIT,
    )


@transaction.atomic
def create_geo_panel_version(
    *,
    actor,
    product,
    panel_key: str,
    version_number: int,
    market_code: str,
    language_code: str,
) -> tuple[GEOProbePanel, bool]:
    """Append one explicitly numbered GEO panel version, replay-safely."""

    _geo_panel_management_grant(actor=actor, product=product)
    normalized = {
        "panel_key": panel_key.strip(),
        "version_number": version_number,
        "product_id": product.pk,
        "market_code": market_code.strip().upper(),
        "language_code": language_code.strip().lower(),
    }
    if not normalized["panel_key"]:
        raise ValidationError({"panel_key": "Panel code cannot be blank."})

    # The product lock serialises two browser submissions which target the
    # same natural key. The model's (panel_key, version_number) uniqueness is
    # the final database guard on PostgreSQL.
    type(product).objects.select_for_update().get(pk=product.pk)
    existing = GEOProbePanel.objects.filter(
        panel_key=normalized["panel_key"],
        version_number=version_number,
    ).first()
    if existing:
        existing_payload = {
            "panel_key": existing.panel_key,
            "version_number": existing.version_number,
            "product_id": existing.product_id,
            "market_code": existing.market_code,
            "language_code": existing.language_code,
        }
        if existing_payload != normalized:
            raise ValidationError(
                "This GEO panel version already exists with different immutable input."
            )
        return existing, False

    return (
        GEOProbePanel.objects.create(
            panel_key=normalized["panel_key"],
            version_number=version_number,
            product=product,
            market_code=normalized["market_code"],
            language_code=normalized["language_code"],
            created_by_principal=actor,
        ),
        True,
    )


@transaction.atomic
def add_geo_panel_item(
    *,
    actor,
    panel: GEOProbePanel,
    item_number: int,
    question: str,
    intent: str,
) -> tuple[GEOProbePanelItem, bool]:
    """Append one typed question to an exact immutable panel version."""

    _geo_panel_management_grant(actor=actor, product=panel.product)
    normalized_question = question.strip()
    normalized_intent = intent.strip()
    if not normalized_question:
        raise ValidationError({"question": "Question cannot be blank."})

    GEOProbePanel.objects.select_for_update().get(pk=panel.pk)
    existing = GEOProbePanelItem.objects.filter(
        panel=panel,
        item_number=item_number,
    ).first()
    if existing:
        if (existing.question, existing.intent) != (
            normalized_question,
            normalized_intent,
        ):
            raise ValidationError(
                "This question number already exists with different immutable input."
            )
        return existing, False

    return (
        GEOProbePanelItem.objects.create(
            panel=panel,
            item_number=item_number,
            question=normalized_question,
            intent=normalized_intent,
        ),
        True,
    )


def _metric(*, key: str, name: str, domain: str, unit: str, actor) -> MetricDefinition:
    latest = MetricDefinition.objects.filter(metric_key=key).order_by("-version_number").first()
    if latest:
        if latest.data_domain != domain:
            raise ValidationError({"metric_key": "同名指标已经属于另一个数据域，已拒绝混用。"})
        if unit and latest.unit and latest.unit != unit:
            raise ValidationError({"unit": "该指标的单位与已封存定义不一致。"})
        return latest
    return MetricDefinition.objects.create(
        metric_key=key,
        version_number=1,
        name=name,
        data_domain=domain,
        value_kind=MetricDefinition.ValueKind.COUNT,
        unit=unit,
        created_by_principal=actor,
    )


def record_performance_rows(
    *,
    actor,
    channel_account,
    rows: list[dict],
    source_kind: str,
    operation_key: str,
    publication=None,
):
    if source_kind not in {MetricCollectionRun.SourceKind.MANUAL, MetricCollectionRun.SourceKind.CSV}:
        raise ValidationError({"source_kind": "反馈工作台只接受人工输入或粘贴 CSV。"})
    if not rows or len(rows) > 100:
        raise ValidationError({"rows": "一次必须提交 1 到 100 行。"})
    grant = _collection_grant(actor=actor, account=channel_account)
    if publication is not None:
        publication = Publication.objects.select_related("current_gate__channel_account").get(
            pk=publication.pk
        )
        if publication.status != Publication.Status.MANUAL_PUBLISHED_RECORDED:
            raise ValidationError({"publication": "只能为已经记录发布证明的内容追加表现。"})
        if not publication.current_gate_id:
            raise ValidationError({"publication": "发布记录缺少确切 Release Gate。"})
        if publication.current_gate.channel_account_id != channel_account.pk:
            raise ValidationError({"publication": "发布记录与平台账号不一致。"})
    payload_hash = _sha256(
        {
            "account": channel_account.account_code,
            "publication_id": str(publication.pk) if publication else "",
            "source_kind": source_kind,
            "rows": rows,
        }
    )
    run_key = f"feedback-performance-{operation_key}"
    observed_times = [row["observed_at"] for row in rows]
    window_start = min(observed_times)
    window_end = max(observed_times) + timedelta(microseconds=1)
    with transaction.atomic():
        # Serialise submissions for one account so a double-click with the same
        # operation key cannot create two append-only fact sets on PostgreSQL.
        type(channel_account).objects.select_for_update().get(pk=channel_account.pk)
        existing = MetricCollectionRun.objects.filter(run_key=run_key).first()
        if existing:
            if existing.parameters.get("payload_sha256") != payload_hash:
                raise ValidationError("同一个提交编号不能用于不同内容。")
            relation = (
                existing.insights_publicationperformanceobservation_observations
                if publication
                else existing.insights_channelperformanceobservation_observations
            )
            return list(relation.all())
        run = MetricCollectionRun.objects.create(
            run_key=run_key,
            data_domain=DataDomain.CONTENT_PERFORMANCE,
            source_kind=source_kind,
            source_reference=(
                f"feedback-ui:publication:{publication.pk}"
                if publication
                else "feedback-ui:channel-account"
            ),
            parameters={
                "payload_sha256": payload_hash,
                "permission_grant_id": str(grant.pk),
                "interface": "feedback-ui",
                "publication_id": str(publication.pk) if publication else "",
            },
            window_start=window_start,
            window_end=window_end,
            status=MetricCollectionRun.Status.COMPLETED,
            started_at=timezone.now(),
            completed_at=timezone.now(),
            created_by_principal=actor,
        )
        observations = []
        linked_metrics = set()
        for row in rows:
            metric = _metric(
                key=row["metric_key"],
                name=row["metric_name"],
                domain=DataDomain.CONTENT_PERFORMANCE,
                unit=row.get("unit", ""),
                actor=actor,
            )
            if metric.pk not in linked_metrics:
                MetricCollectionRunMetric.objects.create(
                    collection_run=run,
                    metric_definition=metric,
                    data_domain=DataDomain.CONTENT_PERFORMANCE,
                )
                linked_metrics.add(metric.pk)
            common = {
                "metric_definition": metric,
                "collection_run": run,
                "data_domain": DataDomain.CONTENT_PERFORMANCE,
                "availability_state": row["availability_state"],
                "numeric_value": row["numeric_value"],
                "unit": row.get("unit", ""),
                "observed_at": row["observed_at"],
                "source_reference": row.get("source_reference", ""),
                "recorded_by_principal": actor,
            }
            if publication:
                observations.append(
                    PublicationPerformanceObservation.objects.create(
                        publication=publication,
                        **common,
                    )
                )
            else:
                observations.append(
                    ChannelPerformanceObservation.objects.create(
                        channel_account=channel_account,
                        **common,
                    )
                )
        return observations


def record_geo_result(
    *,
    actor,
    panel_item,
    provider: str,
    model_reference: str,
    availability_state: str,
    response_text: str,
    brand_mentioned: bool,
    rank_position: int | None,
    citation_urls: list[str],
    operation_key: str,
):
    product = panel_item.panel.product
    is_test_seed = is_local_test_geo_item(panel_item)
    grant = _product_grant(
        actor=actor,
        product=product,
        action=PermissionGrant.Action.COLLECT_READ_ONLY,
    )
    payload_hash = _sha256(
        {
            "panel_item": str(panel_item.pk),
            "provider": provider,
            "model_reference": model_reference,
            "availability_state": availability_state,
            "response_text": response_text,
            "brand_mentioned": brand_mentioned,
            "rank_position": rank_position,
            "citation_urls": citation_urls,
        }
    )
    run_key = f"feedback-geo-{operation_key}"
    now = timezone.now()
    with transaction.atomic():
        type(panel_item.panel).objects.select_for_update().get(pk=panel_item.panel_id)
        existing = GEOProbeRun.objects.filter(run_key=run_key).first()
        if existing:
            if existing.parameters.get("payload_sha256") != payload_hash:
                raise ValidationError("同一个提交编号不能用于不同内容。")
            return existing.results.get(panel_item=panel_item)
        probe_run = GEOProbeRun.objects.create(
            run_key=run_key,
            panel=panel_item.panel,
            provider=provider,
            model_reference=model_reference,
            parameters={
                "payload_sha256": payload_hash,
                "permission_grant_id": str(grant.pk),
                "interface": "feedback-ui",
                "is_local_test_seed": is_test_seed,
                "question_source": (
                    "LOCAL_SYSTEM_PRESET" if is_test_seed else "FORMAL_PANEL"
                ),
            },
            status=(
                GEOProbeRun.Status.COMPLETED
                if availability_state == AvailabilityState.PRESENT
                else GEOProbeRun.Status.PARTIAL
            ),
            started_at=now,
            completed_at=now,
            created_by_principal=actor,
        )
        result = GEOProbeResult.objects.create(
            probe_run=probe_run,
            panel_item=panel_item,
            availability_state=availability_state,
            response_text=response_text,
            brand_mentioned=brand_mentioned if availability_state == AvailabilityState.PRESENT else False,
            rank_position=rank_position if availability_state == AvailabilityState.PRESENT else None,
            recorded_at=now,
        )
        for number, url in enumerate(citation_urls, start=1):
            host = (urlparse(url).hostname or "").lower()
            GEOProbeCitation.objects.create(
                result=result,
                citation_number=number,
                cited_url=url,
                cited_domain=host,
            )

        # A local system preset is a UI practice run, not business evidence.
        # Keep the clearly tagged question/answer fact so the person can learn
        # the workflow, but do not create a formal metric run or observation.
        # This makes it structurally impossible for the practice result to
        # enter Demand, Performance, formal GEO, or Learning evidence chains.
        if is_test_seed:
            return result

        metric = _metric(
            key="geo-brand-mentioned",
            name="GEO 回答提及品牌",
            domain=DataDomain.GEO,
            unit="boolean",
            actor=actor,
        )
        metric_run = MetricCollectionRun.objects.create(
            run_key=f"feedback-geo-metric-{operation_key}",
            data_domain=DataDomain.GEO,
            source_kind=MetricCollectionRun.SourceKind.MANUAL,
            source_reference=f"geo-probe:{result.pk}",
            parameters={
                "payload_sha256": payload_hash,
                "permission_grant_id": str(grant.pk),
                "interface": "feedback-ui",
                "is_local_test_seed": is_test_seed,
                "question_source": (
                    "LOCAL_SYSTEM_PRESET" if is_test_seed else "FORMAL_PANEL"
                ),
            },
            window_start=now,
            window_end=now + timedelta(microseconds=1),
            status=MetricCollectionRun.Status.COMPLETED,
            started_at=now,
            completed_at=now,
            created_by_principal=actor,
        )
        MetricCollectionRunMetric.objects.create(
            collection_run=metric_run,
            metric_definition=metric,
            data_domain=DataDomain.GEO,
        )
        GEOMetricObservation.objects.create(
            probe_result=result,
            metric_definition=metric,
            collection_run=metric_run,
            data_domain=DataDomain.GEO,
            availability_state=availability_state,
            numeric_value=(
                Decimal("1") if brand_mentioned else Decimal("0")
            ) if availability_state == AvailabilityState.PRESENT else None,
            unit="boolean",
            observed_at=now,
            source_reference=f"geo-probe:{result.pk}",
            recorded_by_principal=actor,
        )
        return result


def evidence_choices(*, actor, limit: int = 80):
    channel = ChannelPerformanceObservation.objects.select_related(
        "channel_account", "metric_definition"
    ).order_by("-created_at")[:limit]
    geo = GEOMetricObservation.objects.select_related(
        "probe_result__panel_item__panel__product", "metric_definition"
    ).exclude(
        Q(probe_result__panel_item__panel__panel_key=LOCAL_TEST_GEO_PANEL_KEY)
        | Q(probe_result__panel_item__intent__istartswith="LOCAL_")
    ).order_by("-created_at")[:limit]
    choices = []
    for item in channel:
        plans = ChannelPlan.objects.select_related("initiative__product").filter(
            channel_account=item.channel_account
        )
        allowed = any(
            resolve_authorization(
                principal=actor,
                acting_role=actor.role,
                action=PermissionGrant.Action.EDIT,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                product=plan.initiative.product,
            ).allowed
            for plan in plans
        )
        if not allowed:
            continue
        value = "缺失" if item.numeric_value is None else str(item.numeric_value)
        choices.append(
            (f"channel:{item.pk}", f"平台表现｜{item.channel_account.account_code}｜{item.metric_definition.name}={value}")
        )
    for item in geo:
        product = item.probe_result.panel_item.panel.product
        if not resolve_authorization(
            principal=actor,
            acting_role=actor.role,
            action=PermissionGrant.Action.EDIT,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=product,
        ).allowed:
            continue
        value = "缺失" if item.numeric_value is None else str(item.numeric_value)
        choices.append(
            (f"geo:{item.pk}", f"GEO｜{item.probe_result.panel_item.panel.product.name}｜品牌提及={value}")
        )
    return choices


def _resolve_learning_evidence(*, product, evidence_ref: str):
    try:
        kind, identifier = evidence_ref.split(":", 1)
    except ValueError as error:
        raise ValidationError({"evidence_ref": "证据编号无效。"}) from error
    if kind == "geo":
        evidence = GEOMetricObservation.objects.select_related(
            "probe_result__panel_item__panel"
        ).get(pk=identifier)
        if _is_local_test_geo_item(evidence.probe_result.panel_item):
            raise ValidationError(
                {"evidence_ref": "本地 GEO 测试结果不能进入正式学习或需求。"}
            )
        if evidence.probe_result.panel_item.panel.product_id != product.pk:
            raise ValidationError({"evidence_ref": "GEO 证据不属于所选产品。"})
        return LearningEvidenceLink.SourceKind.GEO, evidence
    if kind == "channel":
        evidence = ChannelPerformanceObservation.objects.select_related("channel_account").get(pk=identifier)
        if not ChannelPlan.objects.filter(
            initiative__product=product,
            channel_account=evidence.channel_account,
        ).exists():
            raise ValidationError({"evidence_ref": "平台表现证据没有与该产品的 Channel Plan 建立关系。"})
        return LearningEvidenceLink.SourceKind.CHANNEL_PERFORMANCE, evidence
    raise ValidationError({"evidence_ref": "不支持的证据类型。"})


def propose_learning(
    *,
    actor,
    product,
    learning_key: str,
    title: str,
    conclusion: str,
    recommended_action: str,
    confidence,
    evidence_ref: str,
    evidence_note: str,
):
    _product_grant(actor=actor, product=product, action=PermissionGrant.Action.EDIT)
    source_kind, evidence = _resolve_learning_evidence(product=product, evidence_ref=evidence_ref)
    stable_key = f"manual-{product.product_code.lower()}-{learning_key}"
    with transaction.atomic():
        type(product).objects.select_for_update().get(pk=product.pk)
        tip = LearningVersion.objects.select_for_update().filter(
            learning_key=stable_key,
            product=product,
        ).order_by("-version_number").first()
        if tip and (
            tip.status == LearningVersion.Status.PROPOSED
            and tip.title == title
            and tip.conclusion == conclusion
            and tip.recommended_action == recommended_action
            and tip.confidence == confidence
        ):
            exact_link_exists = (
                tip.evidence_links.filter(channel_performance=evidence).exists()
                if source_kind == LearningEvidenceLink.SourceKind.CHANNEL_PERFORMANCE
                else tip.evidence_links.filter(geo_metric=evidence).exists()
            )
            if exact_link_exists:
                return tip
        learning = LearningVersion.objects.create(
            learning_key=stable_key,
            version_number=(tip.version_number + 1 if tip else 1),
            product=product,
            title=title,
            conclusion=conclusion,
            recommended_action=recommended_action,
            confidence=confidence,
            status=LearningVersion.Status.PROPOSED,
            supersedes_version=tip,
            created_by_principal=actor,
        )
        link_kwargs = {
            "learning_version": learning,
            "source_kind": source_kind,
            "evidence_note": evidence_note,
        }
        if source_kind == LearningEvidenceLink.SourceKind.CHANNEL_PERFORMANCE:
            link_kwargs["channel_performance"] = evidence
        else:
            link_kwargs["geo_metric"] = evidence
        LearningEvidenceLink.objects.create(**link_kwargs)
        return learning
