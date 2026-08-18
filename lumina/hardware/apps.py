from django.apps import AppConfig


class HardwareConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "lumina.hardware"
    label = "hardware"

    def ready(self) -> None:
        # Deleting a listing needs its dependent rows gone before Django's collector tries to
        # null their foreign keys. See the module docstring: it is a MySQL/MariaDB-only failure,
        # so nothing but a real delete on a real engine exercises it.
        from lumina.hardware import signals  # noqa: F401
