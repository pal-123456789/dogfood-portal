# src/awards/urls.py
"""URL routes for the prizes/awards feature (namespace: "awards").

All routes are event-scoped under events/<event_ext_id>/awards. The organizer console lives at
.../awards/manage and its POST actions under .../awards/prizes/<prize_ext_id>/...; the PUBLIC
podium page is the bare .../awards. These are wired in by the project include later -- nothing
here is linked from base.html.
"""
from django.urls import path

from . import views

app_name = "awards"

urlpatterns = [
    path("events/<str:event_ext_id>/awards/manage", views.manage, name="manage"),
    path("events/<str:event_ext_id>/awards/topup", views.topup, name="topup"),
    path("events/<str:event_ext_id>/awards/prizes/<str:prize_ext_id>/assign",
         views.assign, name="assign"),
    path("events/<str:event_ext_id>/awards/prizes/<str:prize_ext_id>/clear",
         views.clear, name="clear"),
    path("events/<str:event_ext_id>/awards/prizes/<str:prize_ext_id>/remove",
         views.remove, name="remove"),
    path("events/<str:event_ext_id>/awards", views.podium, name="podium"),
]
