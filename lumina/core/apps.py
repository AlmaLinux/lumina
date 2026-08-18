from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "lumina.core"
    label = "core"

    def ready(self) -> None:
        # Registers the startup checks. Email is the one that matters: every message goes out
        # through a systemd timer with a retry ladder, so a relay that cannot be reached fails
        # where nobody is looking. See the module docstring.
        from lumina.core import checks  # noqa: F401
