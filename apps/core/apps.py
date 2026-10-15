import logging
import os
import sys

from django.apps import AppConfig

logger = logging.getLogger("oscar.config")


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"
    verbose_name = "Core platform"

    def ready(self):
        """Startup log of the mounted module set.

        Runs after LOGGING is configured (settings import is too early for a
        log line to reach any handler). Under ``runserver`` the autoreloader's
        parent process is skipped so the line prints once per (re)start.
        """
        from django.conf import settings

        argv = sys.argv[1:2]
        if argv == ["runserver"] and "--noreload" not in sys.argv and os.environ.get("RUN_MAIN") != "true":
            return
        logger.info(
            "Enabled modules: %s (HRMS_ONLY=%s)",
            ", ".join(settings.ENABLED_MODULES),
            settings.HRMS_ONLY,
        )
