# src/judging/urls.py
from django.urls import path

from . import views

app_name = "judging"
urlpatterns = [
    # Human judge scoring. Deliberately NOT one of the five flat checker routes in
    # portal/urls.py -- those stay byte-stable; this lives under the /judging/ include.
    path("score", views.score, name="score"),
    # Organizer control room (organizer-only, event-scoped by ext_id). Also under the /judging/
    # include, so the five flat checker routes stay byte-stable and replay stays 7/7. The literal
    # "score" above has no trailing segment, and UI/seed event ids are evt_* -- never a bare
    # word -- so none of these <ext_id>/... patterns can shadow it.
    path("<str:ext_id>/progress", views.progress, name="progress"),
    path("<str:ext_id>/assignments", views.assignments, name="assignments"),
    path("<str:ext_id>/assignments/add", views.assign, name="assign"),
    path("<str:ext_id>/assignments/remove", views.unassign, name="unassign"),
    path("<str:ext_id>/auto-assign", views.auto_assign, name="auto_assign"),
    path("<str:ext_id>/rubric", views.rubric, name="rubric"),
]
