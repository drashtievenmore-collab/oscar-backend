"""Management command to evaluate alerts and exceptions across all tenants (PDF §19).

Can be scheduled via cron or Windows Task Scheduler to run every 15 minutes and end-of-day.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounts.models import Client
from apps.core.alert_services import ensure_alert_settings, run_all_alert_checks


class Command(BaseCommand):
    help = "Run the 12 business exception alert checks across tenants."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=None, help="Tenant slug to run alerts for. If omitted, runs for all clients.")
        parser.add_argument("--as-of", default=None, help="Evaluation date in YYYY-MM-DD format (defaults to today).")

    def handle(self, *args, **options):
        clients = Client.objects.filter(status="Active")
        if options["tenant"]:
            clients = clients.filter(slug=options["tenant"])

        as_of = None
        if options["as_of"]:
            from datetime import date
            as_of = date.fromisoformat(options["as_of"])
        else:
            as_of = timezone.localdate()

        self.stdout.write(f"Running alert checks for as_of={as_of} across {clients.count()} tenant(s)...")

        total_incidents = 0
        for client in clients:
            ensure_alert_settings(client)
            incidents = run_all_alert_checks(client.id, as_of=as_of)
            total_incidents += len(incidents)
            self.stdout.write(
                self.style.SUCCESS(f"  Tenant '{client.name}' ({client.slug}): {len(incidents)} alert(s) generated.")
            )

        self.stdout.write(self.style.SUCCESS(f"Alert check run complete. Total new alerts: {total_incidents}."))
