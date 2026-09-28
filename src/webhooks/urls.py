# src/webhooks/urls.py
"""Outbound-webhooks routes, included at /webhooks/ by portal/urls.py (wired by the orchestrator).

RELATIVE paths only (no leading prefix). None of these is among the acceptance checker's five flat
routes and none is linked from base.html, so replay stays untouched. Event ids are `evt_<uuid16>`,
so a `<event_ext_id>` segment can never collide with a literal path segment here.
"""
from django.urls import path

from . import views

app_name = "webhooks"

urlpatterns = [
    path("<str:event_ext_id>/endpoints", views.endpoints, name="endpoints"),
    path("<str:event_ext_id>/endpoints/<str:endpoint_ext_id>/delete",
         views.delete_endpoint, name="delete_endpoint"),
    path("<str:event_ext_id>/deliver", views.deliver, name="deliver"),
    path("<str:event_ext_id>/deliveries/<str:delivery_ext_id>/retry",
         views.retry, name="retry"),
]
