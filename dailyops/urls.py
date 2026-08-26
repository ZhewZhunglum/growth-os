from django.urls import path

from dailyops import views


app_name = "dailyops"

urlpatterns = [
    path("", views.home, name="home"),
    path("sources/setup/", views.source_setup, name="source-setup"),
    path("start/", views.batch_start, name="batch-start"),
    path("<uuid:product_id>/<uuid:batch_key>/", views.batch_detail, name="batch-detail"),
    path(
        "<uuid:product_id>/<uuid:batch_key>/dispose/",
        views.batch_dispose,
        name="batch-dispose",
    ),
    path(
        "<uuid:product_id>/<uuid:batch_key>/collect/automatic/",
        views.automatic_collect,
        name="automatic-collect",
    ),
    path(
        "<uuid:product_id>/<uuid:batch_key>/collect/<str:platform_code>/",
        views.platform_collect,
        name="platform-collect",
    ),
    path(
        "<uuid:product_id>/<uuid:batch_key>/evidence/manual/",
        views.evidence_manual_unified,
        name="evidence-manual-unified",
    ),
    path(
        "<uuid:product_id>/<uuid:batch_key>/evidence/csv/",
        views.evidence_csv_unified,
        name="evidence-csv-unified",
    ),
    path(
        "<uuid:product_id>/<uuid:batch_key>/evidence/<uuid:evidence_id>/remove/",
        views.evidence_invalidate,
        name="evidence-invalidate",
    ),
    path(
        "<uuid:product_id>/<uuid:batch_key>/evidence/<uuid:evidence_id>/correct/",
        views.evidence_correct,
        name="evidence-correct",
    ),
    path(
        "<uuid:product_id>/<uuid:batch_key>/<str:platform_code>/manual/",
        views.evidence_manual,
        name="evidence-manual",
    ),
    path(
        "<uuid:product_id>/<uuid:batch_key>/<str:platform_code>/csv/",
        views.evidence_csv,
        name="evidence-csv",
    ),
    path(
        "<uuid:product_id>/<uuid:batch_key>/analysis/propose/",
        views.analysis_propose,
        name="analysis-propose",
    ),
    path(
        "<uuid:product_id>/<uuid:batch_key>/analysis/<uuid:proposal_id>/accept/",
        views.analysis_accept,
        name="analysis-accept",
    ),
    path(
        "<uuid:product_id>/<uuid:batch_key>/analysis/<uuid:proposal_id>/accept-and-start/",
        views.analysis_accept_and_start,
        name="analysis-accept-and-start",
    ),
    path(
        "opportunities/<uuid:opportunity_id>/transition/",
        views.opportunity_transition,
        name="opportunity-transition",
    ),
    path(
        "opportunities/<uuid:opportunity_id>/initiative/",
        views.initiative_create,
        name="initiative-create",
    ),
    path(
        "initiatives/<uuid:initiative_id>/transition/",
        views.initiative_transition,
        name="initiative-transition",
    ),
    path(
        "initiatives/<uuid:initiative_id>/plans/",
        views.plan_create,
        name="plan-create",
    ),
    path("plans/<uuid:plan_id>/transition/", views.plan_transition, name="plan-transition"),
    path("plans/<uuid:plan_id>/compile/", views.plan_compile, name="plan-compile"),
    path(
        "plans/<uuid:plan_id>/confirm-and-compile/",
        views.plan_confirm_and_compile,
        name="plan-confirm-and-compile",
    ),
]
