from django.contrib import admin

from .models import (
    ChannelPerformanceObservation,
    CommerceObservation,
    GEOMetricObservation,
    GEOProbeCitation,
    GEOProbePanel,
    GEOProbePanelItem,
    GEOProbeResult,
    GEOProbeRun,
    LearningEvidenceLink,
    LearningVersion,
    MetricCollectionRun,
    MetricCollectionRunMetric,
    MetricDefinition,
    PublicationPerformanceObservation,
    SearchIndexObservation,
    SearchProperty,
    SearchVisibilityObservation,
)


class AppendOnlyAdmin(admin.ModelAdmin):
    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in (
    MetricDefinition,
    MetricCollectionRun,
    MetricCollectionRunMetric,
    SearchProperty,
    PublicationPerformanceObservation,
    ChannelPerformanceObservation,
    SearchVisibilityObservation,
    SearchIndexObservation,
    CommerceObservation,
    GEOProbePanel,
    GEOProbePanelItem,
    GEOProbeRun,
    GEOProbeResult,
    GEOProbeCitation,
    GEOMetricObservation,
    LearningVersion,
    LearningEvidenceLink,
):
    admin.site.register(model, AppendOnlyAdmin)
