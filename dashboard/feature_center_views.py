from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from dashboard.feature_center import build_feature_center


@login_required
def feature_center(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "dashboard/feature_center.html",
        {"feature_center": build_feature_center(request.user)},
    )
