# src/portal/urls.py
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path

urlpatterns = [
    path("healthz", lambda r: HttpResponse("ok", content_type="text/plain")),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("", include("gallery.urls")),
    path("events/", include("events.urls")),
    path("submissions/", include("submissions.urls")),
    path("judging/", include("judging.urls")),
    path("normalize/", include("normalize.urls")),
]
