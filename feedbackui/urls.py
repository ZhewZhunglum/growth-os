from django.urls import path

from . import views


app_name = "feedback"

urlpatterns = [
    path("", views.feedback_home, name="home"),
    path("performance/manual/", views.performance_manual, name="performance-manual"),
    path("performance/csv/", views.performance_csv, name="performance-csv"),
    path("geo/panels/", views.geo_panel_version, name="geo-panel-version"),
    path("geo/panels/items/", views.geo_panel_item, name="geo-panel-item"),
    path("geo/result/", views.geo_result, name="geo-result"),
    path("learning/propose/", views.learning_proposal, name="learning-proposal"),
]
