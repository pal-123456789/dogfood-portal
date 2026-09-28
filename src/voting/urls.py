# src/voting/urls.py
"""Community-voting routes, included at /voting/ by portal/urls.py (wired by the orchestrator).

None of these are among the acceptance checker's five flat routes and none are linked from
base.html, so replay stays untouched. `confirm/<token>` is listed first so the literal `confirm`
segment is never shadowed by the `<event_ext_id>/...` patterns (event ids are `evt_<uuid16>`, so
they can never equal a literal segment here anyway)."""
from django.urls import path

from . import views

app_name = "voting"

urlpatterns = [
    path("confirm/<str:token>", views.confirm, name="confirm"),
    path("<str:event_ext_id>/manage", views.manage, name="manage"),
    path("<str:event_ext_id>/ballot", views.ballot, name="ballot"),
    path("<str:event_ext_id>/join", views.join, name="join"),
    path("<str:event_ext_id>/results", views.results, name="results"),
    path("<str:event_ext_id>/publish", views.publish, name="publish"),
]
