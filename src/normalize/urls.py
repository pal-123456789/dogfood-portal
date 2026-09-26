# src/normalize/urls.py
from django.urls import path

from . import views

app_name = "normalize"

urlpatterns = [
    path("", views.leaderboard, name="leaderboard"),
    path("leaderboard.json", views.leaderboard_json, name="leaderboard_json"),
]
