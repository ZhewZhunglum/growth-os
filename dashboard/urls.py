from django.urls import path

from dashboard.review_views import (
    release_detail,
    release_done_action,
    release_gate_action,
    release_proof_action,
    release_queue,
    review_action,
    review_detail,
    review_queue,
)
from dashboard.views import home, task_action, task_create, task_detail


app_name = "dashboard"
urlpatterns = [
    path("", home, name="home"),
    path("tasks/new/", task_create, name="task-create"),
    path("tasks/<uuid:task_id>/", task_detail, name="task-detail"),
    path("tasks/<uuid:task_id>/actions/<str:action>/", task_action, name="task-action"),
    path("review/", review_queue, name="review-queue"),
    path("review/<uuid:task_id>/", review_detail, name="review-detail"),
    path("review/<uuid:task_id>/action/", review_action, name="review-action"),
    path("release/", release_queue, name="release-queue"),
    path("release/<uuid:task_id>/", release_detail, name="release-detail"),
    path("release/<uuid:task_id>/gate/", release_gate_action, name="release-gate-action"),
    path("release/<uuid:task_id>/proof/", release_proof_action, name="release-proof-action"),
    path("release/<uuid:task_id>/done/", release_done_action, name="release-done-action"),
]
