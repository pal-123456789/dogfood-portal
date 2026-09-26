# src/portal/urls.py
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path

from gallery import views as gallery_views
from judging import views as judging_views
from submissions import views as submission_views

urlpatterns = [
    path("healthz", lambda r: HttpResponse("ok", content_type="text/plain")),

    # Flat, byte-exact routes the acceptance checker hits (no trailing slash, not under an
    # app prefix). Kept together here so the checker contract is legible in one place.
    path("projects", gallery_views.projects),                  # checks 1 & 2
    path("projects/new", submission_views.submit),             # check 3 (4xx when closed)
    path("api/judge/scores", judging_views.judge_scores),      # checks 4, 5, 6
    path("api/export.csv", judging_views.export_csv),          # check 7
    path("debug/whoami", gallery_views.whoami),                # §4e DEMO-auth proof

    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("", include("gallery.urls")),
    path("events/", include("events.urls")),
    path("submissions/", include("submissions.urls")),
    path("judging/", include("judging.urls")),
    path("normalize/", include("normalize.urls")),
]
