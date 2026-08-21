from django.contrib.auth import views as auth_views
from django.urls import include, path


urlpatterns = [
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("daily/", include("dailyops.urls")),
    path("", include("dashboard.urls")),
]
