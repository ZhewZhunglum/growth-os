from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core.views import health


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("i18n/", include("django.conf.urls.i18n")),
    path("daily/", include("dailyops.urls")),
    path("feedback/", include("feedbackui.urls")),
    path("governance/", include("governanceui.urls")),
    path("", include("dashboard.urls")),
]

