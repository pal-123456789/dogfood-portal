# src/comments/urls.py
"""Project-comments routes, mounted at /comments/ by portal/urls.py (the orchestrator wires the
include). None of these are on the five flat acceptance-checker routes and none are linked from
base.html, so replay stays 7/7. The `projects/<ext_id>` read/post route is listed before the
`<ext_id>/moderate` route so a project path can never be shadowed by the moderation pattern.
"""
from django.urls import path

from . import views

app_name = "comments"

urlpatterns = [
    path("projects/<str:ext_id>", views.project_comments, name="project_comments"),
    path("<str:ext_id>/moderate", views.moderate, name="moderate"),
]
