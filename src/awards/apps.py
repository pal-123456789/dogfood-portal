# src/awards/apps.py
from django.apps import AppConfig


class AwardsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "awards"
    verbose_name = "Prizes and awards"
