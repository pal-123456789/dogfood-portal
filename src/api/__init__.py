# src/api/__init__.py
# Read-only public REST API (/api/v1/). No models and no migrations live here; the app label
# exists only so `manage.py test api` resolves and DRF/drf-spectacular can find the views.
