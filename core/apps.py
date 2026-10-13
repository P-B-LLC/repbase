from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        from . import checks  # noqa: F401 — registers the deploy checks
        from . import account_controls  # noqa: F401 — session revocation receiver
        from . import media_cleanup  # noqa: F401 — registers deletion receivers
