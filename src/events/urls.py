# src/events/urls.py
"""Organizer UI routes, included at /events/ AFTER the five flat checker routes in
portal/urls.py. `new` and the invitee `invite/<ext_id>` redeem route are listed before the
`<str:ext_id>` catch so they are never shadowed; UI-minted ids are `evt_<uuid16>` (events) and
`inv_<uuid16>` (invites), so they never equal a literal segment here. None of these are on the
checker's flat routes and none are linked from base.html, so replay stays 7/7."""
from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("new", views.create_event, name="create"),
    path("invite/<str:ext_id>", views.redeem, name="redeem"),
    path("<str:ext_id>", views.detail, name="detail"),
    path("<str:ext_id>/tracks/new", views.create_track, name="create_track"),
    path("<str:ext_id>/teams/new", views.create_team, name="create_team"),
    path("<str:ext_id>/state", views.set_state, name="set_state"),
    path("<str:ext_id>/invites/new", views.create_invite, name="create_invite"),
]
