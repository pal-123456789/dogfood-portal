# src/events/urls.py
"""Organizer UI routes, included at /events/ AFTER the five flat checker routes in
portal/urls.py. `new` is listed before the `<str:ext_id>` catch so it is never shadowed;
UI-minted ids are `evt_<uuid16>`, so they never equal a literal segment here."""
from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("new", views.create_event, name="create"),
    path("<str:ext_id>", views.detail, name="detail"),
    path("<str:ext_id>/tracks/new", views.create_track, name="create_track"),
    path("<str:ext_id>/teams/new", views.create_team, name="create_team"),
    path("<str:ext_id>/state", views.set_state, name="set_state"),
]
