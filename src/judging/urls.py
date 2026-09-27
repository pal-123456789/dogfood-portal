# src/judging/urls.py
from django.urls import path

from . import views

app_name = "judging"
urlpatterns = [
    # Human judge scoring. Deliberately NOT one of the five flat checker routes in
    # portal/urls.py -- those stay byte-stable; this lives under the /judging/ include.
    path("score", views.score, name="score"),
]
