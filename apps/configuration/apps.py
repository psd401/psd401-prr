# ABOUTME: Django app config for the in-app settings store.
from django.apps import AppConfig


class ConfigurationConfig(AppConfig):
    name = "apps.configuration"
    label = "configuration"
    verbose_name = "District configuration"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Registers the setting_changed receiver that keeps the bootstrap
        # fallback honest under override_settings.
        from . import runtime  # noqa: F401
