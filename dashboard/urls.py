from django.urls import path

from dashboard.review_views import (
    release_detail,
    release_done_action,
    release_gate_action,
    release_proof_action,
    release_queue,
    review_action,
    review_detail,
    review_history_detail,
    review_queue,
)
from dashboard.views import home, task_action, task_create, task_detail
from dashboard.release_actions import release_rework_action, release_stop_action
from dashboard.feature_center_views import feature_center
from dashboard.guide_views import guide
from dashboard.config_views import (
    configuration_home,
    product_configuration,
    product_configuration_action,
    runtime_configuration,
    runtime_configuration_action,
)
from dashboard.team_views import (
    change_my_password,
    team_grant_issue,
    team_grant_renew,
    team_grant_revoke,
    team_member_detail,
    team_members,
)


app_name = "dashboard"
urlpatterns = [
    path("", home, name="home"),
    path("features/", feature_center, name="feature-center"),
    path("guide/", guide, name="guide"),
    path("tasks/new/", task_create, name="task-create"),
    path("tasks/<uuid:task_id>/", task_detail, name="task-detail"),
    path("tasks/<uuid:task_id>/actions/<str:action>/", task_action, name="task-action"),
    path("review/", review_queue, name="review-queue"),
    path("review/<uuid:task_id>/", review_detail, name="review-detail"),
    path("review/<uuid:task_id>/action/", review_action, name="review-action"),
    path("review/history/<uuid:review_id>/", review_history_detail, name="review-history-detail"),
    path("release/", release_queue, name="release-queue"),
    path("release/<uuid:task_id>/", release_detail, name="release-detail"),
    path("release/<uuid:task_id>/gate/", release_gate_action, name="release-gate-action"),
    path("release/<uuid:task_id>/proof/", release_proof_action, name="release-proof-action"),
    path("release/<uuid:task_id>/done/", release_done_action, name="release-done-action"),
    path("release/<uuid:task_id>/stop/", release_stop_action, name="release-stop-action"),
    path("release/<uuid:task_id>/rework/", release_rework_action, name="release-rework-action"),
    path("team/", team_members, name="team-members"),
    path("team/<uuid:member_id>/", team_member_detail, name="team-member-detail"),
    path("team/<uuid:member_id>/grants/new/", team_grant_issue, name="team-grant-issue"),
    path("team/grants/<uuid:grant_id>/renew/", team_grant_renew, name="team-grant-renew"),
    path("team/grants/<uuid:grant_id>/revoke/", team_grant_revoke, name="team-grant-revoke"),
    path("account/password/", change_my_password, name="change-my-password"),
    path("configuration/", configuration_home, name="configuration-home"),
    path("configuration/products/<uuid:product_id>/", product_configuration, name="product-configuration"),
    path(
        "configuration/products/<uuid:product_id>/<str:action>/",
        product_configuration_action,
        name="product-configuration-action",
    ),
    path("configuration/runtime/", runtime_configuration, name="runtime-configuration"),
    path(
        "configuration/runtime/<str:action>/",
        runtime_configuration_action,
        name="runtime-configuration-action",
    ),
]
