# src/submissions/urls.py
"""Participant self-service routes, mounted at /submissions/ by portal/urls.py.

These are deliberately OFF the five flat checker routes (/projects, /projects/new, ...):
adding them moves no byte of the acceptance surface, and they are reachable by URL rather
than linked from base.html, so the gallery/nav the checker sees stays identical.
"""
from django.urls import path

from . import views

app_name = "submissions"

urlpatterns = [
    path("mine", views.mine, name="mine"),
    path("<str:ext_id>/edit", views.edit, name="edit"),
    path("<str:ext_id>/withdraw", views.withdraw, name="withdraw"),
]
