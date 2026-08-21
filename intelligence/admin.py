from django.contrib import admin

from intelligence import models


class ImmutableAuditAdmin(admin.ModelAdmin):
    actions = None

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.SourceRegistry)
class SourceRegistryAdmin(ImmutableAuditAdmin):
    list_display = ("source_key", "version_number", "platform_code", "source_kind", "status")
    list_filter = ("platform_code", "source_kind", "status")
    search_fields = ("source_key", "display_name")


@admin.register(models.CollectionRun)
class CollectionRunAdmin(ImmutableAuditAdmin):
    list_display = ("batch_key", "source", "attempt_number", "status", "availability_state", "started_at")
    list_filter = ("status", "availability_state", "source__platform_code")


@admin.register(models.ExternalEvidenceItem)
class ExternalEvidenceItemAdmin(ImmutableAuditAdmin):
    list_display = ("title", "platform_code", "market_code", "observed_at", "source")
    list_filter = ("platform_code", "market_code", "language_code")
    search_fields = ("title", "excerpt", "external_url", "external_content_id")


@admin.register(models.Topic)
class TopicAdmin(ImmutableAuditAdmin):
    list_display = ("topic_key", "version_number", "label", "market_code", "decision_state")
    list_filter = ("market_code", "language_code", "decision_state")
    search_fields = ("topic_key", "label", "summary")


@admin.register(models.ProductOpportunity)
class ProductOpportunityAdmin(admin.ModelAdmin):
    list_display = ("opportunity_key", "product", "title", "priority_score", "current_state", "state_version")
    list_filter = ("current_state", "risk_level", "product")
    readonly_fields = tuple(field.name for field in models.ProductOpportunity._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.Initiative)
class InitiativeAdmin(admin.ModelAdmin):
    list_display = ("initiative_key", "product", "title", "target_date", "current_state", "state_version")
    list_filter = ("current_state", "product")
    readonly_fields = tuple(field.name for field in models.Initiative._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.ChannelPlan)
class ChannelPlanAdmin(admin.ModelAdmin):
    list_display = ("plan_key", "platform_code", "plan_date", "current_state", "state_version")
    list_filter = ("platform_code", "current_state", "plan_date")
    readonly_fields = tuple(field.name for field in models.ChannelPlan._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(
    [
        models.RawArtifact,
        models.RawArtifactParse,
        models.EvidenceArtifactLink,
        models.SignalAssessment,
        models.TopicEvidenceLink,
        models.ProductTopicFitAssessment,
        models.DemandAssessment,
        models.DemandEvidenceLink,
        models.OpportunityStateEvent,
        models.InitiativeStateEvent,
        models.ChannelPlanRole,
        models.ChannelPlanStateEvent,
        models.TaskCompilationContext,
    ],
    ImmutableAuditAdmin,
)


@admin.register(models.ProductTopicFit)
class ProductTopicFitAdmin(ImmutableAuditAdmin):
    list_display = ("product", "topic", "created_at")
