from django.urls import path

from governanceui import views


app_name = "governanceui"

urlpatterns = [
    path("", views.home, name="home"),
    path("issues/new/", views.issue_create, name="issue-create"),
    path("issues/<uuid:issue_id>/", views.issue_detail, name="issue-detail"),
    path("issues/<uuid:issue_id>/transition/", views.issue_transition, name="issue-transition"),
    path("meetings/new/", views.meeting_create, name="meeting-create"),
    path("meetings/<uuid:meeting_id>/", views.meeting_detail, name="meeting-detail"),
    path("meetings/<uuid:meeting_id>/decisions/new/", views.meeting_decision_create, name="meeting-decision-create"),
    path("policies/new/", views.policy_definition_create, name="policy-definition-create"),
    path("policies/versions/new/", views.policy_version_create, name="policy-version-create"),
    path("rules/new/", views.proposal_create, name="proposal-create"),
    path("rules/<uuid:proposal_id>/", views.proposal_detail, name="proposal-detail"),
    path("rules/<uuid:proposal_id>/validate/", views.proposal_validate, name="proposal-validate"),
    path("rules/<uuid:proposal_id>/approve/", views.proposal_approve, name="proposal-approve"),
    path("rules/<uuid:proposal_id>/activate/", views.proposal_activate, name="proposal-activate"),
    path("activations/<uuid:activation_id>/rollback/", views.activation_rollback, name="activation-rollback"),
]
