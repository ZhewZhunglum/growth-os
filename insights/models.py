from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import UUIDv7Model


class DataDomain(models.TextChoices):
    CONTENT_PERFORMANCE = "CONTENT_PERFORMANCE", "Content performance"
    SEARCH_VISIBILITY = "SEARCH_VISIBILITY", "Search visibility"
    COMMERCE_OUTCOME = "COMMERCE_OUTCOME", "Commerce outcome"
    PROCESS_TELEMETRY = "PROCESS_TELEMETRY", "Process telemetry"
    GEO = "GEO", "Generative-engine visibility"


class AvailabilityState(models.TextChoices):
    PRESENT = "PRESENT", "Present"
    MISSING = "MISSING", "Missing"
    BLOCKED = "BLOCKED", "Blocked"
    UNAVAILABLE = "UNAVAILABLE", "Unavailable"
    INVALIDATED = "INVALIDATED", "Invalidated"


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Insight facts are append-only; create a correction instead.")

    def delete(self):
        raise ValidationError("Insight facts are append-only and cannot be deleted.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Insight facts cannot be bulk-updated.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        raise ValidationError("Bulk creation bypasses insight-fact validation.")


class AppendOnlyManager(models.Manager.from_queryset(AppendOnlyQuerySet)):
    pass


class AppendOnlyFact(UUIDv7Model):
    objects = AppendOnlyManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(f"{self.__class__.__name__} is immutable; append a new fact.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f"{self.__class__.__name__} is append-only.")


class MetricDefinition(AppendOnlyFact):
    class ValueKind(models.TextChoices):
        COUNT = "COUNT", "Count"
        RATIO = "RATIO", "Ratio"
        CURRENCY = "CURRENCY", "Currency"
        SCORE = "SCORE", "Score"
        DURATION = "DURATION", "Duration"

    metric_key = models.CharField(max_length=120)
    version_number = models.PositiveIntegerField()
    name = models.CharField(max_length=240)
    description = models.TextField(blank=True)
    data_domain = models.CharField(max_length=32, choices=DataDomain.choices)
    value_kind = models.CharField(max_length=16, choices=ValueKind.choices)
    unit = models.CharField(max_length=32, blank=True)
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["metric_key", "version_number"], name="insights_unique_metric_version"),
            models.CheckConstraint(condition=Q(version_number__gte=1), name="insights_metric_version_gte_one"),
            models.CheckConstraint(condition=~Q(metric_key=""), name="insights_metric_key_not_empty"),
        ]

    def __str__(self):
        return f"{self.metric_key} v{self.version_number}"


class MetricCollectionRun(AppendOnlyFact):
    class SourceKind(models.TextChoices):
        API = "API", "API"
        BROWSER = "BROWSER", "Browser"
        CSV = "CSV", "CSV"
        MANUAL = "MANUAL", "Manual"
        SYSTEM = "SYSTEM", "System"

    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"

    run_key = models.CharField(max_length=160, unique=True)
    data_domain = models.CharField(max_length=32, choices=DataDomain.choices)
    source_kind = models.CharField(max_length=16, choices=SourceKind.choices)
    source_reference = models.CharField(max_length=512, blank=True)
    parameters = models.JSONField(default=dict, blank=True)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField()
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(window_end__gt=models.F("window_start")), name="insights_run_window_valid"),
            models.CheckConstraint(condition=Q(completed_at__gte=models.F("started_at")), name="insights_run_time_valid"),
        ]


class MetricCollectionRunMetric(AppendOnlyFact):
    collection_run = models.ForeignKey(MetricCollectionRun, on_delete=models.PROTECT, related_name="metric_links")
    metric_definition = models.ForeignKey(MetricDefinition, on_delete=models.PROTECT, related_name="collection_run_links")
    data_domain = models.CharField(max_length=32, choices=DataDomain.choices)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["collection_run", "metric_definition"], name="insights_unique_run_metric"),
        ]

    def clean(self):
        super().clean()
        if self.collection_run_id and self.data_domain != self.collection_run.data_domain:
            raise ValidationError({"data_domain": "Run-metric link must use the run's exact data domain."})
        if self.metric_definition_id and self.data_domain != self.metric_definition.data_domain:
            raise ValidationError({"metric_definition": "Metric definition belongs to another data domain."})


class SearchProperty(AppendOnlyFact):
    class Provider(models.TextChoices):
        GOOGLE_SEARCH_CONSOLE = "GOOGLE_SEARCH_CONSOLE", "Google Search Console"
        GOOGLE_SEARCH = "GOOGLE_SEARCH", "Google Search"

    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="search_properties")
    property_key = models.CharField(max_length=255)
    provider = models.CharField(max_length=32, choices=Provider.choices)
    canonical_url = models.URLField(max_length=1024)
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "property_key"], name="insights_unique_search_property"),
        ]


class ObservationBase(AppendOnlyFact):
    metric_definition = models.ForeignKey(
        MetricDefinition,
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_observations",
    )
    collection_run = models.ForeignKey(
        MetricCollectionRun,
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_observations",
    )
    data_domain = models.CharField(max_length=32, choices=DataDomain.choices)
    availability_state = models.CharField(max_length=16, choices=AvailabilityState.choices)
    numeric_value = models.DecimalField(max_digits=28, decimal_places=8, null=True, blank=True)
    unit = models.CharField(max_length=32, blank=True)
    dimensions = models.JSONField(default=dict, blank=True)
    observed_at = models.DateTimeField()
    source_reference = models.CharField(max_length=1024, blank=True)
    supersedes_observation = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by_observation",
    )
    correction_reason = models.TextField(blank=True)
    recorded_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        abstract = True

    def supersession_partition(self) -> tuple:
        return ()

    def clean(self):
        super().clean()
        errors: dict[str, str] = {}
        if self.availability_state == AvailabilityState.PRESENT and self.numeric_value is None:
            errors["numeric_value"] = "PRESENT requires a numeric value; zero is a valid value."
        if self.availability_state != AvailabilityState.PRESENT and self.numeric_value is not None:
            errors["numeric_value"] = "Only PRESENT may carry a numeric value."
        if self.availability_state == AvailabilityState.INVALIDATED and not self.supersedes_observation_id:
            errors["supersedes_observation"] = "INVALIDATED must point to the exact fact it invalidates."
        if self.supersedes_observation_id and not self.correction_reason.strip():
            errors["correction_reason"] = "A correction or invalidation requires a reason."
        if self.metric_definition_id and self.metric_definition.data_domain != self.data_domain:
            errors["metric_definition"] = "Observation metric belongs to another data domain."
        if self.collection_run_id and self.collection_run.data_domain != self.data_domain:
            errors["collection_run"] = "Observation collection run belongs to another data domain."
        if self.collection_run_id and self.metric_definition_id and not MetricCollectionRunMetric.objects.filter(
            collection_run_id=self.collection_run_id,
            metric_definition_id=self.metric_definition_id,
            data_domain=self.data_domain,
        ).exists():
            errors["metric_definition"] = "The exact metric must be declared by the exact collection run."
        previous = self.supersedes_observation if self.supersedes_observation_id else None
        if previous and (
            previous.data_domain != self.data_domain
            or previous.metric_definition_id != self.metric_definition_id
            or previous.supersession_partition() != self.supersession_partition()
        ):
            errors["supersedes_observation"] = "Corrections must remain in the exact metric and subject partition."
        if errors:
            raise ValidationError(errors)


class PublicationPerformanceObservation(ObservationBase):
    publication = models.ForeignKey("releasegate.Publication", on_delete=models.PROTECT, related_name="performance_observations")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(data_domain=DataDomain.CONTENT_PERFORMANCE), name="insights_publication_obs_domain"),
            models.CheckConstraint(
                condition=(Q(availability_state=AvailabilityState.PRESENT, numeric_value__isnull=False) | (~Q(availability_state=AvailabilityState.PRESENT) & Q(numeric_value__isnull=True))),
                name="insights_publication_obs_availability",
            ),
        ]

    def supersession_partition(self):
        return (self.publication_id,)


class ChannelPerformanceObservation(ObservationBase):
    channel_account = models.ForeignKey("releasegate.ChannelAccount", on_delete=models.PROTECT, related_name="performance_observations")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(data_domain=DataDomain.CONTENT_PERFORMANCE), name="insights_channel_obs_domain"),
            models.CheckConstraint(
                condition=(Q(availability_state=AvailabilityState.PRESENT, numeric_value__isnull=False) | (~Q(availability_state=AvailabilityState.PRESENT) & Q(numeric_value__isnull=True))),
                name="insights_channel_obs_availability",
            ),
        ]

    def supersession_partition(self):
        return (self.channel_account_id,)


class SearchVisibilityObservation(ObservationBase):
    search_property = models.ForeignKey(SearchProperty, on_delete=models.PROTECT, related_name="visibility_observations")
    query_text = models.CharField(max_length=512)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(data_domain=DataDomain.SEARCH_VISIBILITY), name="insights_search_visibility_domain"),
            models.CheckConstraint(
                condition=(Q(availability_state=AvailabilityState.PRESENT, numeric_value__isnull=False) | (~Q(availability_state=AvailabilityState.PRESENT) & Q(numeric_value__isnull=True))),
                name="insights_search_visibility_availability",
            ),
        ]

    def supersession_partition(self):
        return (self.search_property_id, self.query_text)


class SearchIndexObservation(ObservationBase):
    search_property = models.ForeignKey(SearchProperty, on_delete=models.PROTECT, related_name="index_observations")
    inspected_url = models.URLField(max_length=1024)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(data_domain=DataDomain.SEARCH_VISIBILITY), name="insights_search_index_domain"),
            models.CheckConstraint(
                condition=(Q(availability_state=AvailabilityState.PRESENT, numeric_value__isnull=False) | (~Q(availability_state=AvailabilityState.PRESENT) & Q(numeric_value__isnull=True))),
                name="insights_search_index_availability",
            ),
        ]

    def supersession_partition(self):
        return (self.search_property_id, self.inspected_url)


class CommerceObservation(ObservationBase):
    class Touchpoint(models.TextChoices):
        PRODUCT_VIEW = "PRODUCT_VIEW", "Product view"
        ADD_TO_CART = "ADD_TO_CART", "Add to cart"
        CHECKOUT = "CHECKOUT", "Checkout"
        PURCHASE = "PURCHASE", "Purchase"
        REVENUE = "REVENUE", "Revenue"

    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="commerce_observations")
    touchpoint = models.CharField(max_length=24, choices=Touchpoint.choices)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(data_domain=DataDomain.COMMERCE_OUTCOME), name="insights_commerce_obs_domain"),
            models.CheckConstraint(
                condition=(Q(availability_state=AvailabilityState.PRESENT, numeric_value__isnull=False) | (~Q(availability_state=AvailabilityState.PRESENT) & Q(numeric_value__isnull=True))),
                name="insights_commerce_obs_availability",
            ),
        ]

    def supersession_partition(self):
        return (self.product_id, self.touchpoint)


class GEOProbePanel(AppendOnlyFact):
    panel_key = models.CharField(max_length=120)
    version_number = models.PositiveIntegerField()
    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="geo_probe_panels")
    market_code = models.CharField(max_length=16)
    language_code = models.CharField(max_length=16)
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["panel_key", "version_number"], name="insights_unique_geo_panel_version"),
            models.CheckConstraint(condition=Q(version_number__gte=1), name="insights_geo_panel_version_gte_one"),
        ]


class GEOProbePanelItem(AppendOnlyFact):
    panel = models.ForeignKey(GEOProbePanel, on_delete=models.PROTECT, related_name="items")
    item_number = models.PositiveIntegerField()
    question = models.TextField()
    intent = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["panel", "item_number"], name="insights_unique_geo_panel_item"),
            models.CheckConstraint(condition=Q(item_number__gte=1), name="insights_geo_item_number_gte_one"),
        ]


class GEOProbeRun(AppendOnlyFact):
    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"

    run_key = models.CharField(max_length=160, unique=True)
    panel = models.ForeignKey(GEOProbePanel, on_delete=models.PROTECT, related_name="runs")
    provider = models.CharField(max_length=64)
    model_reference = models.CharField(max_length=160)
    parameters = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField()
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(completed_at__gte=models.F("started_at")), name="insights_geo_run_time_valid"),
        ]


class GEOProbeResult(AppendOnlyFact):
    probe_run = models.ForeignKey(GEOProbeRun, on_delete=models.PROTECT, related_name="results")
    panel_item = models.ForeignKey(GEOProbePanelItem, on_delete=models.PROTECT, related_name="results")
    availability_state = models.CharField(max_length=16, choices=AvailabilityState.choices)
    response_text = models.TextField(blank=True)
    brand_mentioned = models.BooleanField(default=False)
    rank_position = models.PositiveIntegerField(null=True, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["probe_run", "panel_item"], name="insights_unique_geo_run_item"),
        ]

    def clean(self):
        super().clean()
        if self.probe_run_id and self.panel_item_id and self.probe_run.panel_id != self.panel_item.panel_id:
            raise ValidationError({"panel_item": "GEO result item must belong to the run's exact panel version."})
        if self.availability_state == AvailabilityState.PRESENT and not self.response_text.strip():
            raise ValidationError({"response_text": "A PRESENT GEO result requires response text."})
        if self.availability_state != AvailabilityState.PRESENT and self.response_text:
            raise ValidationError({"response_text": "Unavailable GEO results cannot contain answer text."})


class GEOProbeCitation(AppendOnlyFact):
    result = models.ForeignKey(GEOProbeResult, on_delete=models.PROTECT, related_name="citations")
    citation_number = models.PositiveIntegerField()
    cited_url = models.URLField(max_length=1024)
    cited_title = models.CharField(max_length=512, blank=True)
    cited_domain = models.CharField(max_length=255)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["result", "citation_number"], name="insights_unique_geo_citation_number"),
            models.CheckConstraint(condition=Q(citation_number__gte=1), name="insights_geo_citation_number_gte_one"),
        ]


class GEOMetricObservation(ObservationBase):
    probe_result = models.ForeignKey(GEOProbeResult, on_delete=models.PROTECT, related_name="metric_observations")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(data_domain=DataDomain.GEO), name="insights_geo_metric_domain"),
            models.CheckConstraint(
                condition=(Q(availability_state=AvailabilityState.PRESENT, numeric_value__isnull=False) | (~Q(availability_state=AvailabilityState.PRESENT) & Q(numeric_value__isnull=True))),
                name="insights_geo_metric_availability",
            ),
        ]

    def supersession_partition(self):
        return (self.probe_result_id,)


class LearningVersion(AppendOnlyFact):
    class Status(models.TextChoices):
        PROPOSED = "PROPOSED", "Proposed"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    learning_key = models.CharField(max_length=160)
    version_number = models.PositiveIntegerField()
    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="learning_versions")
    title = models.CharField(max_length=240)
    conclusion = models.TextField()
    recommended_action = models.TextField()
    confidence = models.DecimalField(max_digits=5, decimal_places=4)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROPOSED)
    supersedes_version = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by_version"
    )
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["learning_key", "version_number"], name="insights_unique_learning_version"),
            models.CheckConstraint(condition=Q(version_number__gte=1), name="insights_learning_version_gte_one"),
            models.CheckConstraint(condition=Q(confidence__gte=Decimal("0")) & Q(confidence__lte=Decimal("1")), name="insights_learning_confidence_range"),
        ]

    def clean(self):
        super().clean()
        if self.supersedes_version_id and (
            self.supersedes_version.learning_key != self.learning_key
            or self.supersedes_version.product_id != self.product_id
            or self.supersedes_version.version_number >= self.version_number
        ):
            raise ValidationError({"supersedes_version": "Learning revisions must extend the same learning chain."})


class LearningEvidenceLink(AppendOnlyFact):
    class SourceKind(models.TextChoices):
        PUBLICATION_PERFORMANCE = "PUBLICATION_PERFORMANCE", "Publication performance"
        CHANNEL_PERFORMANCE = "CHANNEL_PERFORMANCE", "Channel performance"
        SEARCH_VISIBILITY = "SEARCH_VISIBILITY", "Search visibility"
        SEARCH_INDEX = "SEARCH_INDEX", "Search index"
        COMMERCE = "COMMERCE", "Commerce"
        GEO = "GEO", "GEO"

    learning_version = models.ForeignKey(LearningVersion, on_delete=models.PROTECT, related_name="evidence_links")
    source_kind = models.CharField(max_length=32, choices=SourceKind.choices)
    publication_performance = models.ForeignKey(PublicationPerformanceObservation, null=True, blank=True, on_delete=models.PROTECT, related_name="learning_links")
    channel_performance = models.ForeignKey(ChannelPerformanceObservation, null=True, blank=True, on_delete=models.PROTECT, related_name="learning_links")
    search_visibility = models.ForeignKey(SearchVisibilityObservation, null=True, blank=True, on_delete=models.PROTECT, related_name="learning_links")
    search_index = models.ForeignKey(SearchIndexObservation, null=True, blank=True, on_delete=models.PROTECT, related_name="learning_links")
    commerce = models.ForeignKey(CommerceObservation, null=True, blank=True, on_delete=models.PROTECT, related_name="learning_links")
    geo_metric = models.ForeignKey(GEOMetricObservation, null=True, blank=True, on_delete=models.PROTECT, related_name="learning_links")
    evidence_note = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(source_kind="PUBLICATION_PERFORMANCE", publication_performance__isnull=False, channel_performance__isnull=True, search_visibility__isnull=True, search_index__isnull=True, commerce__isnull=True, geo_metric__isnull=True)
                    | Q(source_kind="CHANNEL_PERFORMANCE", publication_performance__isnull=True, channel_performance__isnull=False, search_visibility__isnull=True, search_index__isnull=True, commerce__isnull=True, geo_metric__isnull=True)
                    | Q(source_kind="SEARCH_VISIBILITY", publication_performance__isnull=True, channel_performance__isnull=True, search_visibility__isnull=False, search_index__isnull=True, commerce__isnull=True, geo_metric__isnull=True)
                    | Q(source_kind="SEARCH_INDEX", publication_performance__isnull=True, channel_performance__isnull=True, search_visibility__isnull=True, search_index__isnull=False, commerce__isnull=True, geo_metric__isnull=True)
                    | Q(source_kind="COMMERCE", publication_performance__isnull=True, channel_performance__isnull=True, search_visibility__isnull=True, search_index__isnull=True, commerce__isnull=False, geo_metric__isnull=True)
                    | Q(source_kind="GEO", publication_performance__isnull=True, channel_performance__isnull=True, search_visibility__isnull=True, search_index__isnull=True, commerce__isnull=True, geo_metric__isnull=False)
                ),
                name="insights_learning_evidence_typed_source",
            ),
        ]
