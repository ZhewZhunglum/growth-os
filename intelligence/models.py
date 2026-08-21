from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import TimeStampedModel, UUIDv7Model


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DataDomain(models.TextChoices):
    EXTERNAL_DEMAND = "EXTERNAL_DEMAND", "External demand"


class ActingRole(models.TextChoices):
    OWNER = "OWNER", "Owner"
    OPERATIONS_ADMIN = "OPERATIONS_ADMIN", "Operations admin"
    OPERATOR = "OPERATOR", "Operator"


class AssessmentMethod(models.TextChoices):
    HUMAN = "HUMAN", "Human"
    DETERMINISTIC = "DETERMINISTIC", "Deterministic"
    AI_PROPOSAL = "AI_PROPOSAL", "AI proposal"


class DecisionState(models.TextChoices):
    PROPOSED = "PROPOSED", "Proposed"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class AvailabilityState(models.TextChoices):
    PRESENT = "PRESENT", "Present"
    MISSING = "MISSING", "Missing"
    BLOCKED = "BLOCKED", "Blocked"
    UNAVAILABLE = "UNAVAILABLE", "Unavailable"


class RiskLevel(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"
    CRITICAL = "CRITICAL", "Critical"


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Immutable intelligence facts cannot be updated.")

    def delete(self):
        raise ValidationError("Immutable intelligence facts cannot be deleted.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Immutable intelligence facts cannot be bulk-updated.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        raise ValidationError("Bulk creation bypasses intelligence-fact validation.")


class ImmutableManager(models.Manager.from_queryset(ImmutableQuerySet)):
    pass


class ImmutableFact(UUIDv7Model):
    objects = ImmutableManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(f"{self.__class__.__name__} is immutable; create a new fact.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f"{self.__class__.__name__} is append-only.")


class EventProjectedQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Event-projected aggregates cannot be bulk-updated.")

    def delete(self):
        raise ValidationError("Event-projected aggregates are retained for audit.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Event-projected aggregates cannot be bulk-updated.")


class EventProjectedManager(models.Manager.from_queryset(EventProjectedQuerySet)):
    pass


class EventProjectedAggregate(TimeStampedModel):
    """An aggregate whose state may change only through an appended state event."""

    class Meta:
        abstract = True

    objects = EventProjectedManager()

    _projection_fields = frozenset({"current_state", "state_version", "updated_at"})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            changed = {
                field.attname
                for field in self._meta.concrete_fields
                if field.attname != "id" and getattr(original, field.attname) != getattr(self, field.attname)
            }
            allowed = {"current_state", "state_version", "updated_by_principal_id", "updated_at"}
            if changed and (not getattr(self, "_allow_projection_save", False) or not changed <= allowed):
                raise ValidationError(
                    f"{self.__class__.__name__} is event-projected; append a state event instead of updating it."
                )
        self.full_clean()
        return super().save(*args, **kwargs)


class SourceRegistry(ImmutableFact):
    class SourceKind(models.TextChoices):
        OFFICIAL_API = "OFFICIAL_API", "Official API"
        THIRD_PARTY_API = "THIRD_PARTY_API", "Third-party API"
        BROWSER = "BROWSER", "Browser"
        CSV = "CSV", "CSV"
        MANUAL_LINK = "MANUAL_LINK", "Manual link"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        PAUSED = "PAUSED", "Paused"
        RETIRED = "RETIRED", "Retired"

    source_key = models.CharField(max_length=120)
    version_number = models.PositiveIntegerField()
    platform_code = models.CharField(max_length=64)
    source_kind = models.CharField(max_length=24, choices=SourceKind.choices)
    data_domain = models.CharField(max_length=32, choices=DataDomain.choices, default=DataDomain.EXTERNAL_DEMAND)
    display_name = models.CharField(max_length=240)
    trust_tier = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    non_secret_config = models.JSONField(default=dict, blank=True)
    supersedes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="intelligence_sources_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["source_key", "version_number"]
        constraints = [
            models.UniqueConstraint(fields=["source_key", "version_number"], name="intel_source_key_version_uniq"),
            models.CheckConstraint(condition=Q(version_number__gte=1), name="intel_source_version_gte_one"),
            models.CheckConstraint(
                condition=Q(trust_tier__gte=1) & Q(trust_tier__lte=5), name="intel_source_trust_range"
            ),
            models.CheckConstraint(
                condition=Q(data_domain=DataDomain.EXTERNAL_DEMAND), name="intel_source_external_domain"
            ),
        ]

    def clean(self):
        super().clean()
        if self.supersedes_id and (
            self.supersedes.source_key != self.source_key
            or self.supersedes.version_number >= self.version_number
        ):
            raise ValidationError({"supersedes": "A source may supersede only an older version of the same key."})
        if not isinstance(self.non_secret_config, dict):
            raise ValidationError({"non_secret_config": "Source configuration must be a JSON object."})

    def __str__(self) -> str:
        return f"{self.source_key} v{self.version_number}"


class CollectionRun(ImmutableFact):
    class Status(models.TextChoices):
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        PARTIAL = "PARTIAL", "Partial"
        BLOCKED = "BLOCKED", "Blocked"
        FAILED = "FAILED", "Failed"

    source = models.ForeignKey(SourceRegistry, on_delete=models.PROTECT, related_name="collection_runs")
    data_domain = models.CharField(max_length=32, choices=DataDomain.choices, default=DataDomain.EXTERNAL_DEMAND)
    batch_key = models.UUIDField(db_index=True)
    attempt_number = models.PositiveIntegerField(default=1)
    operation_key = models.UUIDField(unique=True)
    request_payload_hash = models.CharField(max_length=64)
    operation_payload_hash = models.CharField(max_length=64)
    query_spec = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices)
    availability_state = models.CharField(max_length=16, choices=AvailabilityState.choices)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField()
    result_summary = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    executed_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="intelligence_collection_runs"
    )
    permission_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="intelligence_collection_runs"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch_key", "source", "attempt_number"], name="intel_run_batch_source_attempt_uniq"
            ),
            models.CheckConstraint(condition=Q(attempt_number__gte=1), name="intel_run_attempt_gte_one"),
            models.CheckConstraint(condition=Q(completed_at__gte=models.F("started_at")), name="intel_run_time_order"),
            models.CheckConstraint(condition=~Q(request_payload_hash=""), name="intel_run_payload_hash_set"),
            models.CheckConstraint(condition=~Q(operation_payload_hash=""), name="intel_run_operation_hash_set"),
            models.CheckConstraint(
                condition=Q(data_domain=DataDomain.EXTERNAL_DEMAND), name="intel_run_external_domain"
            ),
        ]

    def clean(self):
        super().clean()
        if self.source_id and self.source.data_domain != self.data_domain:
            raise ValidationError({"data_domain": "A run must stay in its source data domain."})
        if self.permission_grant_id and self.executed_by_principal_id:
            if self.permission_grant.principal_id != self.executed_by_principal_id:
                raise ValidationError({"permission_grant": "The run must record the executing principal's grant."})
            if self.permission_grant.action != "COLLECT_READ_ONLY" or self.permission_grant.effect != "ALLOW":
                raise ValidationError({"permission_grant": "Collection requires a COLLECT_READ_ONLY grant."})
            if self.permission_grant.scope_kind == "PLATFORM":
                if self.permission_grant.platform_code != self.source.platform_code:
                    raise ValidationError({"permission_grant": "Platform grant must match the source platform."})
            elif self.permission_grant.scope_kind != "GLOBAL":
                raise ValidationError({"permission_grant": "Collection requires GLOBAL or exact PLATFORM scope."})
        if self.status == self.Status.SUCCEEDED and self.availability_state != AvailabilityState.PRESENT:
            raise ValidationError({"availability_state": "A successful run must report PRESENT data."})


class RawArtifact(ImmutableFact):
    collection_run = models.ForeignKey(CollectionRun, on_delete=models.PROTECT, related_name="raw_artifacts")
    source = models.ForeignKey(SourceRegistry, on_delete=models.PROTECT, related_name="raw_artifacts")
    data_domain = models.CharField(max_length=32, choices=DataDomain.choices, default=DataDomain.EXTERNAL_DEMAND)
    external_url = models.URLField(max_length=2048, blank=True)
    external_content_id = models.CharField(max_length=512, blank=True)
    media_type = models.CharField(max_length=120, default="application/json")
    observed_at = models.DateTimeField()
    captured_at = models.DateTimeField(default=timezone.now)
    payload = models.JSONField(default=dict)
    content_sha256 = models.CharField(max_length=64, blank=True)
    dedupe_key = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~(Q(external_url="") & Q(external_content_id="")), name="intel_artifact_external_ref"
            ),
            models.CheckConstraint(condition=~Q(content_sha256=""), name="intel_artifact_hash_set"),
            models.CheckConstraint(
                condition=Q(data_domain=DataDomain.EXTERNAL_DEMAND), name="intel_artifact_external_domain"
            ),
        ]

    def clean(self):
        super().clean()
        if self.collection_run_id and self.source_id and self.collection_run.source_id != self.source_id:
            raise ValidationError({"source": "Artifact source must equal the collection run source."})
        if self.collection_run_id and self.collection_run.data_domain != self.data_domain:
            raise ValidationError({"data_domain": "Artifact domain must equal the collection run domain."})
        expected = canonical_sha256(self.payload)
        if not self.content_sha256:
            self.content_sha256 = expected
        elif self.content_sha256 != expected:
            raise ValidationError({"content_sha256": "Artifact payload hash mismatch."})


class RawArtifactParse(ImmutableFact):
    class ParseStatus(models.TextChoices):
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"

    raw_artifact = models.ForeignKey(RawArtifact, on_delete=models.PROTECT, related_name="parses")
    parse_version = models.PositiveIntegerField()
    parser_name = models.CharField(max_length=120)
    parser_version = models.CharField(max_length=80)
    status = models.CharField(max_length=16, choices=ParseStatus.choices)
    structured_payload = models.JSONField(default=dict)
    payload_sha256 = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    supersedes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    parsed_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="intelligence_artifact_parses"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["raw_artifact", "parse_version"], name="intel_artifact_parse_version_uniq"),
            models.CheckConstraint(condition=Q(parse_version__gte=1), name="intel_parse_version_gte_one"),
        ]

    def clean(self):
        super().clean()
        if self.supersedes_id and (
            self.supersedes.raw_artifact_id != self.raw_artifact_id
            or self.supersedes.parse_version >= self.parse_version
        ):
            raise ValidationError({"supersedes": "A parse may supersede only an older parse of the same artifact."})
        expected = canonical_sha256(self.structured_payload)
        if not self.payload_sha256:
            self.payload_sha256 = expected
        elif self.payload_sha256 != expected:
            raise ValidationError({"payload_sha256": "Parsed payload hash mismatch."})


class ExternalEvidenceItem(ImmutableFact):
    source = models.ForeignKey(SourceRegistry, on_delete=models.PROTECT, related_name="evidence_items")
    collection_run = models.ForeignKey(CollectionRun, on_delete=models.PROTECT, related_name="evidence_items")
    data_domain = models.CharField(max_length=32, choices=DataDomain.choices, default=DataDomain.EXTERNAL_DEMAND)
    platform_code = models.CharField(max_length=64)
    market_code = models.CharField(max_length=16)
    language_code = models.CharField(max_length=16)
    external_url = models.URLField(max_length=2048, blank=True)
    external_content_id = models.CharField(max_length=512, blank=True)
    title = models.CharField(max_length=500, blank=True)
    excerpt = models.TextField(blank=True)
    facts = models.JSONField(default=dict)
    observed_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True, blank=True)
    provenance_sha256 = models.CharField(max_length=64, blank=True)
    dedupe_key = models.CharField(max_length=128, unique=True)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="external_evidence_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~(Q(external_url="") & Q(external_content_id="")), name="intel_evidence_external_ref"
            ),
            models.CheckConstraint(
                condition=Q(expires_at__isnull=True) | Q(expires_at__gt=models.F("observed_at")),
                name="intel_evidence_expiry_after_seen",
            ),
            models.CheckConstraint(
                condition=Q(data_domain=DataDomain.EXTERNAL_DEMAND), name="intel_evidence_external_domain"
            ),
        ]

    def provenance_payload(self) -> dict[str, Any]:
        return {
            "source_id": str(self.source_id),
            "collection_run_id": str(self.collection_run_id),
            "platform_code": self.platform_code,
            "external_url": self.external_url,
            "external_content_id": self.external_content_id,
            "observed_at": self.observed_at.isoformat(),
            "facts": self.facts,
        }

    def clean(self):
        super().clean()
        if self.collection_run_id and self.source_id and self.collection_run.source_id != self.source_id:
            raise ValidationError({"source": "Evidence source must equal the collection run source."})
        if self.collection_run_id and self.collection_run.data_domain != self.data_domain:
            raise ValidationError({"data_domain": "Evidence must remain in the collection run domain."})
        expected = canonical_sha256(self.provenance_payload())
        if not self.provenance_sha256:
            self.provenance_sha256 = expected
        elif self.provenance_sha256 != expected:
            raise ValidationError({"provenance_sha256": "Evidence provenance hash mismatch."})


class EvidenceInvalidationEvent(ImmutableFact):
    """Append-only removal of evidence from future Daily Operations decisions.

    The original evidence is deliberately left untouched so any already-made
    decision keeps its exact provenance.  User-facing queries exclude an item
    after this event, while historical links continue to resolve.
    """

    evidence_item = models.OneToOneField(
        ExternalEvidenceItem,
        on_delete=models.PROTECT,
        related_name="invalidation_event",
    )
    product = models.ForeignKey(
        "products.Product",
        on_delete=models.PROTECT,
        related_name="evidence_invalidation_events",
    )
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64)
    reason = models.TextField()
    invalidated_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="evidence_invalidations_created",
    )
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey(
        "accounts.PermissionGrant",
        on_delete=models.PROTECT,
        related_name="evidence_invalidation_events",
    )
    invalidated_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    def payload(self) -> dict[str, Any]:
        return {
            "evidence_item_id": str(self.evidence_item_id),
            "product_id": str(self.product_id),
            "reason": self.reason,
        }

    def clean(self):
        super().clean()
        if not self.reason.strip():
            raise ValidationError({"reason": "A plain-language removal reason is required."})
        if self.evidence_item_id and self.product_id:
            batch_product_id = str(self.evidence_item.collection_run.query_spec.get("product_id", ""))
            if batch_product_id != str(self.product_id):
                raise ValidationError({"product": "Evidence removal must keep the exact batch product."})
        if self.permission_grant_id and self.invalidated_by_principal_id:
            grant = self.permission_grant
            if grant.principal_id != self.invalidated_by_principal_id:
                raise ValidationError({"permission_grant": "The grant must belong to the removing Principal."})
            if grant.action != "EDIT" or grant.effect != "ALLOW":
                raise ValidationError({"permission_grant": "Evidence removal requires an ALLOW EDIT grant."})
            if grant.scope_kind == "PRODUCT" and grant.product_id != self.product_id:
                raise ValidationError({"permission_grant": "The EDIT grant must cover the exact product."})
            if grant.scope_kind not in {"GLOBAL", "PRODUCT"}:
                raise ValidationError({"permission_grant": "Evidence removal requires GLOBAL or PRODUCT scope."})
            if self.acting_role != self.invalidated_by_principal.role:
                raise ValidationError({"acting_role": "The acting role must match the removing Principal."})
            if not grant.is_current:
                raise ValidationError({"permission_grant": "Evidence removal requires a current active grant."})
            from accounts.authorization import resolve_authorization
            from accounts.models import PermissionGrant

            decision = resolve_authorization(
                principal=self.invalidated_by_principal,
                acting_role=self.acting_role,
                action=PermissionGrant.Action.EDIT,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                product=self.product,
            )
            if not decision.allowed or decision.grant is None or decision.grant.pk != grant.pk:
                raise ValidationError(
                    {"permission_grant": "The exact grant must be the current fail-closed authorization decision."}
                )
        expected = canonical_sha256(self.payload())
        if not self.payload_hash:
            self.payload_hash = expected
        elif self.payload_hash != expected:
            raise ValidationError({"payload_hash": "Evidence removal payload hash mismatch."})


class EvidenceArtifactLink(ImmutableFact):
    evidence_item = models.ForeignKey(
        ExternalEvidenceItem, on_delete=models.PROTECT, related_name="artifact_links"
    )
    raw_artifact = models.ForeignKey(RawArtifact, on_delete=models.PROTECT, related_name="evidence_links")
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="evidence_artifact_links_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["evidence_item", "raw_artifact"], name="intel_evidence_artifact_uniq")
        ]

    def clean(self):
        super().clean()
        if self.evidence_item_id and self.raw_artifact_id:
            if self.evidence_item.collection_run_id != self.raw_artifact.collection_run_id:
                raise ValidationError("Evidence and artifact must originate from the same collection run.")
            if self.evidence_item.data_domain != self.raw_artifact.data_domain:
                raise ValidationError("Evidence and artifact must remain in the same data domain.")


class SignalAssessment(ImmutableFact):
    evidence_item = models.ForeignKey(
        ExternalEvidenceItem, on_delete=models.PROTECT, related_name="signal_assessments"
    )
    assessment_key = models.CharField(max_length=120)
    version_number = models.PositiveIntegerField()
    signal_type = models.CharField(max_length=80)
    value = models.JSONField(default=dict)
    confidence = models.DecimalField(max_digits=5, decimal_places=4)
    method = models.CharField(max_length=20, choices=AssessmentMethod.choices)
    decision_state = models.CharField(max_length=16, choices=DecisionState.choices, default=DecisionState.PROPOSED)
    rationale = models.TextField(blank=True)
    model_reference = models.CharField(max_length=200, blank=True)
    supersedes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    assessed_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="signal_assessments_created"
    )
    decided_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="signal_assessments_decided",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["evidence_item", "assessment_key", "version_number"],
                name="intel_signal_assessment_version_uniq",
            ),
            models.CheckConstraint(condition=Q(version_number__gte=1), name="intel_signal_version_gte_one"),
            models.CheckConstraint(
                condition=Q(confidence__gte=0) & Q(confidence__lte=1), name="intel_signal_confidence_range"
            ),
            models.CheckConstraint(
                condition=(Q(decision_state="PROPOSED", decided_by_principal__isnull=True))
                | (~Q(decision_state="PROPOSED") & Q(decided_by_principal__isnull=False)),
                name="intel_signal_decision_actor",
            ),
        ]

    def clean(self):
        super().clean()
        if self.supersedes_id and (
            self.supersedes.evidence_item_id != self.evidence_item_id
            or self.supersedes.assessment_key != self.assessment_key
            or self.supersedes.version_number >= self.version_number
        ):
            raise ValidationError({"supersedes": "An assessment may supersede only its prior version."})
        if self.method == AssessmentMethod.AI_PROPOSAL and self.decision_state == DecisionState.APPROVED:
            if self.decided_by_principal_id == self.assessed_by_principal_id:
                raise ValidationError("AI proposals require a separate human decision record.")


class Topic(ImmutableFact):
    topic_key = models.CharField(max_length=120)
    version_number = models.PositiveIntegerField()
    market_code = models.CharField(max_length=16)
    language_code = models.CharField(max_length=16)
    label = models.CharField(max_length=240)
    summary = models.TextField()
    search_intent = models.CharField(max_length=80, blank=True)
    pain_points = models.JSONField(default=list)
    job_to_be_done = models.TextField(blank=True)
    decision_state = models.CharField(max_length=16, choices=DecisionState.choices, default=DecisionState.PROPOSED)
    supersedes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="topics_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["topic_key", "version_number"], name="intel_topic_key_version_uniq"),
            models.CheckConstraint(condition=Q(version_number__gte=1), name="intel_topic_version_gte_one"),
        ]

    def clean(self):
        super().clean()
        if self.supersedes_id and (
            self.supersedes.topic_key != self.topic_key or self.supersedes.version_number >= self.version_number
        ):
            raise ValidationError({"supersedes": "A topic may supersede only an older version of the same key."})


class TopicEvidenceLink(ImmutableFact):
    class Role(models.TextChoices):
        PRIMARY = "PRIMARY", "Primary"
        SUPPORTING = "SUPPORTING", "Supporting"
        CONTRADICTING = "CONTRADICTING", "Contradicting"

    topic = models.ForeignKey(Topic, on_delete=models.PROTECT, related_name="evidence_links")
    evidence_item = models.ForeignKey(ExternalEvidenceItem, on_delete=models.PROTECT, related_name="topic_links")
    linkage_role = models.CharField(max_length=16, choices=Role.choices)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="topic_evidence_links_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["topic", "evidence_item"], name="intel_topic_evidence_uniq")
        ]


class ProductTopicFit(UUIDv7Model):
    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="topic_fits")
    topic = models.ForeignKey(Topic, on_delete=models.PROTECT, related_name="product_fits")
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="product_topic_fits_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["product", "topic"], name="intel_product_topic_fit_uniq")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("ProductTopicFit identity is immutable; append an assessment.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("ProductTopicFit identity is protected.")


class ProductTopicFitAssessment(ImmutableFact):
    product_topic_fit = models.ForeignKey(ProductTopicFit, on_delete=models.PROTECT, related_name="assessments")
    version_number = models.PositiveIntegerField()
    fit_score = models.DecimalField(max_digits=5, decimal_places=4)
    evidence_strength = models.DecimalField(max_digits=5, decimal_places=4)
    method = models.CharField(max_length=20, choices=AssessmentMethod.choices)
    decision_state = models.CharField(max_length=16, choices=DecisionState.choices, default=DecisionState.PROPOSED)
    rationale = models.TextField()
    supersedes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    assessed_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="product_topic_fit_assessments"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product_topic_fit", "version_number"], name="intel_product_fit_assessment_uniq"
            ),
            models.CheckConstraint(condition=Q(version_number__gte=1), name="intel_product_fit_version_gte_one"),
            models.CheckConstraint(condition=Q(fit_score__gte=0) & Q(fit_score__lte=1), name="intel_fit_score_range"),
            models.CheckConstraint(
                condition=Q(evidence_strength__gte=0) & Q(evidence_strength__lte=1),
                name="intel_fit_evidence_range",
            ),
        ]

    def clean(self):
        super().clean()
        if self.supersedes_id and (
            self.supersedes.product_topic_fit_id != self.product_topic_fit_id
            or self.supersedes.version_number >= self.version_number
        ):
            raise ValidationError({"supersedes": "A fit assessment may supersede only its prior version."})


class DemandAssessment(ImmutableFact):
    topic = models.ForeignKey(Topic, on_delete=models.PROTECT, related_name="demand_assessments")
    version_number = models.PositiveIntegerField()
    data_domain = models.CharField(max_length=32, choices=DataDomain.choices, default=DataDomain.EXTERNAL_DEMAND)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    demand_score = models.DecimalField(max_digits=7, decimal_places=4)
    velocity_score = models.DecimalField(max_digits=7, decimal_places=4)
    confidence = models.DecimalField(max_digits=5, decimal_places=4)
    availability_state = models.CharField(max_length=16, choices=AvailabilityState.choices)
    method = models.CharField(max_length=20, choices=AssessmentMethod.choices)
    decision_state = models.CharField(max_length=16, choices=DecisionState.choices, default=DecisionState.PROPOSED)
    rationale = models.TextField(blank=True)
    supersedes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    assessed_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="demand_assessments_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["topic", "version_number"], name="intel_demand_topic_version_uniq"),
            models.CheckConstraint(condition=Q(version_number__gte=1), name="intel_demand_version_gte_one"),
            models.CheckConstraint(condition=Q(window_end__gt=models.F("window_start")), name="intel_demand_window_order"),
            models.CheckConstraint(
                condition=Q(confidence__gte=0) & Q(confidence__lte=1), name="intel_demand_confidence_range"
            ),
            models.CheckConstraint(
                condition=Q(data_domain=DataDomain.EXTERNAL_DEMAND), name="intel_demand_external_domain"
            ),
        ]

    def clean(self):
        super().clean()
        if self.supersedes_id and (
            self.supersedes.topic_id != self.topic_id or self.supersedes.version_number >= self.version_number
        ):
            raise ValidationError({"supersedes": "Demand may supersede only an older assessment of the same topic."})


class DemandEvidenceLink(ImmutableFact):
    demand_assessment = models.ForeignKey(
        DemandAssessment, on_delete=models.PROTECT, related_name="evidence_links"
    )
    evidence_item = models.ForeignKey(
        ExternalEvidenceItem, on_delete=models.PROTECT, related_name="demand_links"
    )
    weight = models.DecimalField(max_digits=5, decimal_places=4, default=1)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="demand_evidence_links_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["demand_assessment", "evidence_item"], name="intel_demand_evidence_uniq"),
            models.CheckConstraint(condition=Q(weight__gt=0) & Q(weight__lte=1), name="intel_demand_weight_range"),
        ]

    def clean(self):
        super().clean()
        if self.demand_assessment_id and self.evidence_item_id:
            if self.demand_assessment.data_domain != self.evidence_item.data_domain:
                raise ValidationError("Only same-domain external evidence may support external demand.")


class ProductOpportunity(EventProjectedAggregate):
    class State(models.TextChoices):
        PROPOSED = "PROPOSED", "Proposed"
        TRIAGED = "TRIAGED", "Triaged"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        PLANNED = "PLANNED", "Planned"
        CLOSED = "CLOSED", "Closed"

    TRANSITIONS = {
        State.PROPOSED: {State.TRIAGED, State.REJECTED},
        State.TRIAGED: {State.APPROVED, State.REJECTED},
        State.APPROVED: {State.PLANNED, State.CLOSED},
        State.REJECTED: set(),
        State.PLANNED: {State.CLOSED},
        State.CLOSED: set(),
    }

    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="opportunities")
    topic = models.ForeignKey(Topic, on_delete=models.PROTECT, related_name="opportunities")
    demand_assessment = models.ForeignKey(
        DemandAssessment, on_delete=models.PROTECT, related_name="opportunities"
    )
    product_topic_fit_assessment = models.ForeignKey(
        ProductTopicFitAssessment, on_delete=models.PROTECT, related_name="opportunities"
    )
    opportunity_key = models.CharField(max_length=120, unique=True)
    title = models.CharField(max_length=240)
    recommendation = models.TextField()
    priority_score = models.DecimalField(max_digits=7, decimal_places=4)
    risk_level = models.CharField(max_length=16, choices=RiskLevel.choices, default=RiskLevel.MEDIUM)
    current_state = models.CharField(max_length=16, choices=State.choices, default=State.PROPOSED)
    state_version = models.PositiveIntegerField(default=0)
    creation_command_id = models.UUIDField(unique=True)
    creation_payload_hash = models.CharField(max_length=64)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="opportunities_created"
    )
    created_under_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="opportunities_created"
    )
    updated_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="opportunities_updated"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(creation_payload_hash=""), name="intel_opp_creation_hash_set"),
            models.CheckConstraint(
                condition=Q(priority_score__gte=0) & Q(priority_score__lte=1), name="intel_opp_priority_range"
            ),
        ]

    def clean(self):
        super().clean()
        if self._state.adding and (self.current_state != self.State.PROPOSED or self.state_version != 0):
            raise ValidationError("Opportunity creation must begin at PROPOSED state version 0.")
        if self.topic_id and self.demand_assessment_id and self.demand_assessment.topic_id != self.topic_id:
            raise ValidationError({"demand_assessment": "Opportunity demand must assess its exact topic version."})
        if self.product_topic_fit_assessment_id:
            fit = self.product_topic_fit_assessment.product_topic_fit
            if fit.product_id != self.product_id or fit.topic_id != self.topic_id:
                raise ValidationError({"product_topic_fit_assessment": "Opportunity fit must match product and topic."})
        _validate_product_edit_grant(
            grant=self.created_under_grant if self.created_under_grant_id else None,
            principal_id=self.created_by_principal_id,
            product_id=self.product_id,
        )


class OpportunityStateEvent(ImmutableFact):
    opportunity = models.ForeignKey(ProductOpportunity, on_delete=models.PROTECT, related_name="state_events")
    sequence = models.PositiveIntegerField()
    from_state = models.CharField(max_length=16, choices=ProductOpportunity.State.choices)
    to_state = models.CharField(max_length=16, choices=ProductOpportunity.State.choices)
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64)
    reason = models.TextField(blank=True)
    principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="opportunity_state_events"
    )
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="opportunity_state_events"
    )
    event_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["opportunity", "sequence"], name="intel_opp_event_sequence_uniq"),
            models.CheckConstraint(condition=Q(sequence__gte=1), name="intel_opp_event_sequence_gte_one"),
            models.CheckConstraint(condition=~Q(payload_hash=""), name="intel_opp_event_hash_set"),
        ]

    def clean(self):
        super().clean()
        _validate_state_event(
            aggregate=self.opportunity if self.opportunity_id else None,
            from_state=self.from_state,
            to_state=self.to_state,
            sequence=self.sequence,
        )
        _validate_product_edit_grant(
            grant=self.permission_grant if self.permission_grant_id else None,
            principal_id=self.principal_id,
            product_id=self.opportunity.product_id if self.opportunity_id else None,
        )


class Initiative(EventProjectedAggregate):
    class State(models.TextChoices):
        PROPOSED = "PROPOSED", "Proposed"
        APPROVED = "APPROVED", "Approved"
        ACTIVE = "ACTIVE", "Active"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    TRANSITIONS = {
        State.PROPOSED: {State.APPROVED, State.CANCELLED},
        State.APPROVED: {State.ACTIVE, State.CANCELLED},
        State.ACTIVE: {State.COMPLETED, State.CANCELLED},
        State.COMPLETED: set(),
        State.CANCELLED: set(),
    }

    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="initiatives")
    opportunity = models.ForeignKey(ProductOpportunity, on_delete=models.PROTECT, related_name="initiatives")
    initiative_key = models.CharField(max_length=120, unique=True)
    title = models.CharField(max_length=240)
    objective = models.TextField()
    target_date = models.DateField(null=True, blank=True)
    current_state = models.CharField(max_length=16, choices=State.choices, default=State.PROPOSED)
    state_version = models.PositiveIntegerField(default=0)
    creation_command_id = models.UUIDField(unique=True)
    creation_payload_hash = models.CharField(max_length=64)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="initiatives_created"
    )
    created_under_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="initiatives_created"
    )
    updated_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="initiatives_updated"
    )

    class Meta:
        constraints = [models.CheckConstraint(condition=~Q(creation_payload_hash=""), name="intel_init_creation_hash_set")]

    def clean(self):
        super().clean()
        if self._state.adding and (self.current_state != self.State.PROPOSED or self.state_version != 0):
            raise ValidationError("Initiative creation must begin at PROPOSED state version 0.")
        if self.opportunity_id and self.product_id != self.opportunity.product_id:
            raise ValidationError({"opportunity": "Initiative and opportunity must belong to the same product."})
        if self._state.adding and self.opportunity_id and self.opportunity.current_state != ProductOpportunity.State.APPROVED:
            raise ValidationError({"opportunity": "An Initiative requires a human-approved Opportunity."})
        _validate_product_edit_grant(
            grant=self.created_under_grant if self.created_under_grant_id else None,
            principal_id=self.created_by_principal_id,
            product_id=self.product_id,
        )


class InitiativeStateEvent(ImmutableFact):
    initiative = models.ForeignKey(Initiative, on_delete=models.PROTECT, related_name="state_events")
    sequence = models.PositiveIntegerField()
    from_state = models.CharField(max_length=16, choices=Initiative.State.choices)
    to_state = models.CharField(max_length=16, choices=Initiative.State.choices)
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64)
    reason = models.TextField(blank=True)
    principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="initiative_state_events"
    )
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="initiative_state_events"
    )
    event_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["initiative", "sequence"], name="intel_init_event_sequence_uniq"),
            models.CheckConstraint(condition=Q(sequence__gte=1), name="intel_init_event_sequence_gte_one"),
            models.CheckConstraint(condition=~Q(payload_hash=""), name="intel_init_event_hash_set"),
        ]

    def clean(self):
        super().clean()
        _validate_state_event(
            aggregate=self.initiative if self.initiative_id else None,
            from_state=self.from_state,
            to_state=self.to_state,
            sequence=self.sequence,
        )
        _validate_product_edit_grant(
            grant=self.permission_grant if self.permission_grant_id else None,
            principal_id=self.principal_id,
            product_id=self.initiative.product_id if self.initiative_id else None,
        )


class ChannelPlan(EventProjectedAggregate):
    class State(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        READY = "READY", "Ready"
        ACTIVE = "ACTIVE", "Active"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    TRANSITIONS = {
        State.DRAFT: {State.READY, State.CANCELLED},
        State.READY: {State.ACTIVE, State.CANCELLED},
        State.ACTIVE: {State.COMPLETED, State.CANCELLED},
        State.COMPLETED: set(),
        State.CANCELLED: set(),
    }

    initiative = models.ForeignKey(Initiative, on_delete=models.PROTECT, related_name="channel_plans")
    channel_account = models.ForeignKey(
        "releasegate.ChannelAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="channel_plans"
    )
    plan_key = models.CharField(max_length=120, unique=True)
    platform_code = models.CharField(max_length=64)
    plan_date = models.DateField()
    goal = models.JSONField(default=dict)
    content_requirements = models.JSONField(default=dict)
    current_state = models.CharField(max_length=16, choices=State.choices, default=State.DRAFT)
    state_version = models.PositiveIntegerField(default=0)
    creation_command_id = models.UUIDField(unique=True)
    creation_payload_hash = models.CharField(max_length=64)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="channel_plans_created"
    )
    created_under_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="channel_plans_created"
    )
    updated_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="channel_plans_updated"
    )

    class Meta:
        constraints = [models.CheckConstraint(condition=~Q(creation_payload_hash=""), name="intel_plan_creation_hash_set")]

    def clean(self):
        super().clean()
        if self._state.adding and (self.current_state != self.State.DRAFT or self.state_version != 0):
            raise ValidationError("ChannelPlan creation must begin at DRAFT state version 0.")
        if self._state.adding and self.initiative_id and self.initiative.current_state not in {
            Initiative.State.APPROVED,
            Initiative.State.ACTIVE,
        }:
            raise ValidationError({"initiative": "A ChannelPlan requires an approved Initiative."})
        if self.channel_account_id and self.channel_account.platform_code != self.platform_code:
            raise ValidationError({"channel_account": "Channel account platform must match the plan platform."})
        _validate_product_edit_grant(
            grant=self.created_under_grant if self.created_under_grant_id else None,
            principal_id=self.created_by_principal_id,
            product_id=self.initiative.product_id if self.initiative_id else None,
        )

    @property
    def product_id(self):
        return self.initiative.product_id


class ChannelPlanRole(ImmutableFact):
    class Role(models.TextChoices):
        PLAN_OWNER = "PLAN_OWNER", "Plan owner"
        EXECUTOR = "EXECUTOR", "Executor"
        REVIEWER = "REVIEWER", "Reviewer"
        PUBLISHER = "PUBLISHER", "Publisher"

    channel_plan = models.ForeignKey(ChannelPlan, on_delete=models.PROTECT, related_name="role_assignments")
    principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="channel_plan_roles"
    )
    plan_role = models.CharField(max_length=16, choices=Role.choices)
    permission_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="channel_plan_roles"
    )
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    assigned_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="channel_plan_roles_assigned"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["channel_plan", "principal", "plan_role", "valid_from"],
                name="intel_plan_role_assignment_uniq",
            ),
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
                name="intel_plan_role_valid_window",
            ),
        ]

    def clean(self):
        super().clean()
        if self.permission_grant_id and self.principal_id and self.permission_grant.principal_id != self.principal_id:
            raise ValidationError({"permission_grant": "Plan role must record the assigned principal's grant."})


class ChannelPlanStateEvent(ImmutableFact):
    channel_plan = models.ForeignKey(ChannelPlan, on_delete=models.PROTECT, related_name="state_events")
    sequence = models.PositiveIntegerField()
    from_state = models.CharField(max_length=16, choices=ChannelPlan.State.choices)
    to_state = models.CharField(max_length=16, choices=ChannelPlan.State.choices)
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64)
    reason = models.TextField(blank=True)
    principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="channel_plan_state_events"
    )
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="channel_plan_state_events"
    )
    event_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["channel_plan", "sequence"], name="intel_plan_event_sequence_uniq"),
            models.CheckConstraint(condition=Q(sequence__gte=1), name="intel_plan_event_sequence_gte_one"),
            models.CheckConstraint(condition=~Q(payload_hash=""), name="intel_plan_event_hash_set"),
        ]

    def clean(self):
        super().clean()
        _validate_state_event(
            aggregate=self.channel_plan if self.channel_plan_id else None,
            from_state=self.from_state,
            to_state=self.to_state,
            sequence=self.sequence,
        )
        _validate_product_edit_grant(
            grant=self.permission_grant if self.permission_grant_id else None,
            principal_id=self.principal_id,
            product_id=self.channel_plan.product_id if self.channel_plan_id else None,
        )


class TaskCompilationContext(ImmutableFact):
    """Exact, sealed inputs used to compile one existing workflow Task.

    Every business input is a typed foreign key to one exact, sealed version.
    Manifest hashes are copied alongside the keys so later audit does not need
    to infer which bytes the compiler trusted.
    """

    task = models.OneToOneField("workflow.Task", on_delete=models.PROTECT, related_name="compilation_context")
    channel_plan = models.ForeignKey(ChannelPlan, on_delete=models.PROTECT, related_name="compilation_contexts")
    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="task_compilation_contexts")
    product_profile_version = models.ForeignKey(
        "products.ProductProfileVersion", on_delete=models.PROTECT, related_name="task_compilation_contexts"
    )
    task_contract_version = models.ForeignKey(
        "workflow.TaskContractVersion", on_delete=models.PROTECT, related_name="compilation_contexts"
    )
    objective_profile_version = models.ForeignKey(
        "products.ObjectiveProfileVersion", on_delete=models.PROTECT, related_name="task_compilation_contexts"
    )
    objective_profile_manifest_sha256 = models.CharField(max_length=64)
    claim_matrix_version = models.ForeignKey(
        "products.ClaimMatrixVersion", on_delete=models.PROTECT, related_name="task_compilation_contexts"
    )
    claim_matrix_manifest_sha256 = models.CharField(max_length=64)
    evidence_library_version = models.ForeignKey(
        "products.EvidenceLibraryVersion", on_delete=models.PROTECT, related_name="task_compilation_contexts"
    )
    evidence_library_manifest_sha256 = models.CharField(max_length=64)
    policy_set_snapshot = models.JSONField(default=list)
    policy_set_sha256 = models.CharField(max_length=64, blank=True)
    capability_state = models.ForeignKey(
        "releasegate.CapabilityState", on_delete=models.PROTECT, related_name="task_compilation_contexts"
    )
    compiler_name = models.CharField(max_length=120)
    compiler_version = models.CharField(max_length=80)
    input_payload_sha256 = models.CharField(max_length=64)
    compilation_command_id = models.UUIDField(unique=True)
    compiled_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_compilation_contexts"
    )
    permission_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="task_compilation_contexts"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(objective_profile_manifest_sha256=""), name="intel_ctx_objective_hash_set"),
            models.CheckConstraint(condition=~Q(claim_matrix_manifest_sha256=""), name="intel_ctx_claim_hash_set"),
            models.CheckConstraint(condition=~Q(evidence_library_manifest_sha256=""), name="intel_ctx_evidence_hash_set"),
            models.CheckConstraint(condition=~Q(policy_set_sha256=""), name="intel_ctx_policy_hash_set"),
            models.CheckConstraint(condition=~Q(input_payload_sha256=""), name="intel_ctx_input_hash_set"),
        ]

    def clean(self):
        super().clean()
        if self.task_id:
            if self.task.product_id != self.product_id:
                raise ValidationError({"product": "Compilation context product must match its task."})
            if self.task.product_profile_version_id != self.product_profile_version_id:
                raise ValidationError({"product_profile_version": "Context must bind the task's exact profile."})
            if self.task.contract_version_id != self.task_contract_version_id:
                raise ValidationError({"task_contract_version": "Context must bind the task's exact contract."})
        if self.channel_plan_id and self.channel_plan.initiative.product_id != self.product_id:
            raise ValidationError({"channel_plan": "Channel plan and task must belong to the same product."})
        if self.channel_plan_id and self.channel_plan.current_state not in {
            ChannelPlan.State.READY,
            ChannelPlan.State.ACTIVE,
        }:
            raise ValidationError({"channel_plan": "Task compilation requires a ready ChannelPlan."})
        for field_name in ("objective_profile_version", "claim_matrix_version", "evidence_library_version"):
            version = getattr(self, field_name, None)
            if version is not None and not version.is_sealed:
                raise ValidationError({field_name: "Task compilation requires an exact sealed version."})
        if self.claim_matrix_version_id and self.claim_matrix_version.product_id != self.product_id:
            raise ValidationError({"claim_matrix_version": "Claim matrix must belong to the task product."})
        if self.evidence_library_version_id and self.evidence_library_version.product_id != self.product_id:
            raise ValidationError({"evidence_library_version": "Evidence library must belong to the task product."})
        profile_context = {
            "objective_profile_version": self.product_profile_version.objective_profile_version_id,
            "claim_matrix_version": self.product_profile_version.claim_matrix_version_id,
            "evidence_library_version": self.product_profile_version.evidence_library_version_id,
        }
        for field_name, exact_profile_version_id in profile_context.items():
            if exact_profile_version_id is None or getattr(self, f"{field_name}_id") != exact_profile_version_id:
                raise ValidationError({field_name: "Context must equal the exact version sealed into ProductProfileVersion."})
        expected_manifests = {
            "objective_profile_manifest_sha256": getattr(self.objective_profile_version, "manifest_sha256", ""),
            "claim_matrix_manifest_sha256": getattr(self.claim_matrix_version, "manifest_sha256", ""),
            "evidence_library_manifest_sha256": getattr(self.evidence_library_version, "manifest_sha256", ""),
        }
        for hash_field, expected_manifest in expected_manifests.items():
            if getattr(self, hash_field) != expected_manifest:
                raise ValidationError({hash_field: "Snapshot hash must equal the exact sealed version manifest."})
        expected_policy_hash = canonical_sha256(self.policy_set_snapshot)
        if not self.policy_set_sha256:
            self.policy_set_sha256 = expected_policy_hash
        elif self.policy_set_sha256 != expected_policy_hash:
            raise ValidationError({"policy_set_sha256": "Policy snapshot hash mismatch."})
        if not isinstance(self.policy_set_snapshot, list):
            raise ValidationError({"policy_set_snapshot": "Policy snapshot must be a list of exact versions."})
        if not self.policy_set_snapshot:
            raise ValidationError({"policy_set_snapshot": "Task compilation requires an exact policy set."})
        from releasegate.models import PolicyVersion

        seen_policy_ids: set[uuid.UUID] = set()
        snapshot_policy_ids: list[uuid.UUID] = []
        for item in self.policy_set_snapshot:
            if not isinstance(item, dict) or set(item) != {"id", "manifest_sha256"}:
                raise ValidationError({"policy_set_snapshot": "Each policy requires only id and manifest_sha256."})
            try:
                policy_id = uuid.UUID(str(item["id"]))
            except (TypeError, ValueError, AttributeError) as error:
                raise ValidationError({"policy_set_snapshot": "Policy id must be a UUID."}) from error
            if policy_id in seen_policy_ids:
                raise ValidationError({"policy_set_snapshot": "Policy versions must not be duplicated."})
            seen_policy_ids.add(policy_id)
            snapshot_policy_ids.append(policy_id)
            policy = PolicyVersion.objects.filter(pk=policy_id).first()
            if policy is None or policy.manifest_sha256 != item["manifest_sha256"]:
                raise ValidationError({"policy_set_snapshot": "Policy snapshot must match an exact stored version."})
        if snapshot_policy_ids != sorted(snapshot_policy_ids, key=str):
            raise ValidationError({"policy_set_snapshot": "Policy versions must be sorted by id."})
        required_policy_ids = set(
            self.task_contract_version.policy_links.filter(required=True).values_list("policy_version_id", flat=True)
        )
        if seen_policy_ids != required_policy_ids:
            raise ValidationError(
                {"policy_set_snapshot": "Policy snapshot must contain exactly the contract's required PolicyVersions."}
            )
        if self.channel_plan_id and not self.channel_plan.channel_account_id:
            raise ValidationError({"channel_plan": "Task compilation requires an exact ChannelAccount."})
        if self.channel_plan_id and self.capability_state_id:
            capability_account_id = self.capability_state.account_environment_binding.channel_account_id
            if capability_account_id != self.channel_plan.channel_account_id:
                raise ValidationError({"capability_state": "Capability snapshot must cover the plan's exact account."})
            if not self.capability_state.account_environment_binding.is_current_at():
                raise ValidationError({"capability_state": "Capability must use the current exact account binding."})
            if not self.capability_state.is_current_open_at():
                raise ValidationError({"capability_state": "Task compilation requires the current OPEN capability."})
        if self.permission_grant_id and self.compiled_by_principal_id:
            if self.permission_grant.principal_id != self.compiled_by_principal_id:
                raise ValidationError({"permission_grant": "Context must record the compiler's exact grant."})
            if self.permission_grant.action != "CREATE_TASK" or self.permission_grant.effect != "ALLOW":
                raise ValidationError({"permission_grant": "Task compilation requires an ALLOW CREATE_TASK grant."})
            if self.permission_grant.scope_kind == "PRODUCT":
                if self.permission_grant.product_id != self.product_id:
                    raise ValidationError({"permission_grant": "CREATE_TASK grant must cover the exact product."})
            elif self.permission_grant.scope_kind != "GLOBAL":
                raise ValidationError({"permission_grant": "Task compilation requires GLOBAL or PRODUCT scope."})


def _validate_product_edit_grant(*, grant, principal_id, product_id) -> None:
    if grant is None or principal_id is None or product_id is None:
        return
    if grant.principal_id != principal_id:
        raise ValidationError({"permission_grant": "The grant must belong to the acting principal."})
    if grant.action != "EDIT" or grant.effect != "ALLOW":
        raise ValidationError({"permission_grant": "Planning state changes require an ALLOW EDIT grant."})
    if grant.scope_kind == "PRODUCT" and grant.product_id != product_id:
        raise ValidationError({"permission_grant": "The EDIT grant must cover the exact product."})
    if grant.scope_kind not in {"GLOBAL", "PRODUCT"}:
        raise ValidationError({"permission_grant": "Planning requires GLOBAL or exact PRODUCT scope."})


def _validate_state_event(*, aggregate, from_state: str, to_state: str, sequence: int) -> None:
    if aggregate is None:
        return
    if from_state != aggregate.current_state:
        raise ValidationError({"from_state": "Event must start from the aggregate's current projected state."})
    if sequence != aggregate.state_version + 1:
        raise ValidationError({"sequence": "Event sequence must be the next aggregate state version."})
    if to_state not in aggregate.TRANSITIONS.get(from_state, set()):
        raise ValidationError({"to_state": "Event does not follow the aggregate state machine."})
