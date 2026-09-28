# src/bundles/urls.py
"""Bundle routes, included by the orchestrator at /bundles/ with app_name "bundles".

All three endpoints are site-administrator-only (see views._site_admin_or_response). None is on
the acceptance checker's flat route set and none is linked from base.html, so they add surface
without moving replay off its green state. Paths are RELATIVE (the orchestrator mounts them under
/bundles/): the export pair carries the event ext_id, `import` takes the event ext_id from the
signed body instead.
"""
from django.urls import path

from . import views

app_name = "bundles"

urlpatterns = [
    path("<str:event_ext_id>/export.json", views.export_json, name="export_json"),
    path("<str:event_ext_id>/export.csv", views.export_csv, name="export_csv"),
    path("import", views.import_bundle, name="import"),
]
