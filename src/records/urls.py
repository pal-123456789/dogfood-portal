# src/records/urls.py
"""Record routes, included by the orchestrator at /records/ with app_name "records".

The `signing_key` view is ALSO wired top-level by the orchestrator at
`/.well-known/dogfood-signing-key`; it is a plain view function with no app dependency, so it works
standalone from either mount. None of these routes is on the acceptance checker's flat set and none
is linked from base.html, so they add surface without moving replay off 7/7.
"""
from django.urls import path

from . import views

app_name = "records"

urlpatterns = [
    path("judge", views.judge_record, name="judge"),
    path("participant", views.participant_record, name="participant"),
    path("verify", views.verify, name="verify"),
    path("signing-key", views.signing_key, name="signing_key"),
]
