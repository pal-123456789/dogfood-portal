# src/normalize/urls.py
from django.urls import path

from . import views

app_name = "normalize"

urlpatterns = [
    path("", views.leaderboard, name="leaderboard"),
    path("leaderboard.json", views.leaderboard_json, name="leaderboard_json"),
    path("results", views.results, name="results"),
    path("results.json", views.results_json, name="results_json"),
    path("results/publish", views.results_publish, name="results_publish"),
    path("diagnostics", views.diagnostics, name="diagnostics"),
    path("diagnostics.json", views.diagnostics_json, name="diagnostics_json"),
]
