"""Soft-delete duplicate parties.

``(client, code)`` is unique, but names are not: a lead converted more than
once (or a create retried with a fresh idempotency key) leaves several live
rows that differ only in code. This command groups live parties by
``(client, type, lower(name))``, keeps the earliest row in each group and
soft-deletes the rest. Soft-deleting frees the code for reuse, because the
``uq_parties_code`` constraint only covers live rows.

Dry-run by default; pass ``--apply`` to change data.

    python manage.py dedupe_parties                  # report only
    python manage.py dedupe_parties --name "v cbvb"  # report one name
    python manage.py dedupe_parties --apply          # soft-delete duplicates
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.db.models.functions import Lower

from apps.masters.models import Party


class Command(BaseCommand):
    help = (
        "Soft-delete duplicate parties (same tenant, type and name), "
        "keeping the earliest-created row. Dry-run unless --apply is given."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually soft-delete. Without it the command only reports.",
        )
        parser.add_argument(
            "--name",
            help='Restrict to one exact name (case-insensitive), e.g. --name "v cbvb".',
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        name = options.get("name")

        parties = Party.objects.live()
        if name:
            parties = parties.filter(name__iexact=name.strip())

        groups = (
            parties.annotate(lower_name=Lower("name"))
            .values("client_id", "type", "lower_name")
            .annotate(rows=Count("id"))
            .filter(rows__gt=1)
            .order_by("client_id", "type", "lower_name")
        )

        if not groups:
            self.stdout.write(self.style.SUCCESS("No duplicate parties found."))
            return

        total_removed = 0
        for group in groups:
            rows = list(
                Party.objects.live()
                .filter(
                    client_id=group["client_id"],
                    type=group["type"],
                    name__iexact=group["lower_name"],
                )
                .order_by("created_at", "code")
            )
            keeper, duplicates = rows[0], rows[1:]
            codes = ", ".join(d.code for d in duplicates)
            self.stdout.write(
                f'"{keeper.name}" ({keeper.type}): keeping {keeper.code} — '
                f"{len(duplicates)} duplicate(s): {codes}"
            )
            if apply_changes:
                # One group per transaction: a failure leaves the other
                # groups settled rather than half of this one undone.
                with transaction.atomic():
                    for dupe in duplicates:
                        dupe.soft_delete()
            total_removed += len(duplicates)

        if apply_changes:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Soft-deleted {total_removed} duplicate "
                    f"part{'y' if total_removed == 1 else 'ies'}."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN — {total_removed} duplicate(s) would be removed. "
                    "Re-run with --apply."
                )
            )
