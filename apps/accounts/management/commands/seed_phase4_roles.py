"""Management command to seed the 12 core Phase 4 roles and default permissions (PDF §16)."""
from django.core.management.base import BaseCommand
from apps.accounts.models import Client
from apps.accounts.permission_catalogue import seed_roles, sync_permissions


class Command(BaseCommand):
    help = "Seed or refresh the 12 core Phase 4 roles and default permission catalogue."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=None, help="Tenant slug to seed. If omitted, seeds for all clients.")

    def handle(self, *args, **options):
        # 1. Sync the global permission catalogue
        created, updated = sync_permissions()
        self.stdout.write(self.style.SUCCESS(f"Permissions catalogue synced: +{created} new, ~{updated} updated."))

        # 2. Seed roles for tenants
        clients = Client.objects.all()
        if options["tenant"]:
            clients = clients.filter(slug=options["tenant"])

        if not clients.exists():
            self.stdout.write(self.style.WARNING("No tenants found to seed roles for."))
            return

        for client in clients:
            roles = seed_roles(client)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully ensured 12 Phase 4 roles for tenant '{client.name}' ({client.slug}). {len(roles)} roles processed."
                )
            )
