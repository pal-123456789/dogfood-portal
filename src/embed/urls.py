# src/embed/urls.py
"""Tidy app-local URL map. The orchestrator wires the exact top-level paths `/embed.js` and
`/embed/<ext_id>` straight to embed.views.embed_js / embed.views.embed_gallery; these names
mirror those so the app is self-describing if ever mounted via include("embed.urls").
"""
from django.urls import path

from . import views

app_name = "embed"

urlpatterns = [
    path("embed.js", views.embed_js, name="js"),
    path("embed/<str:ext_id>", views.embed_gallery, name="gallery"),
]
