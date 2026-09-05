from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        from . import media_cleanup  # noqa: F401 — registers deletion receivers
