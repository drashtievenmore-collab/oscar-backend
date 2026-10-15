"""Seed textile master catalogue for every tenant.

Idempotent: get_or_create on (client, code).
Includes:
- ProcessMaster (Dyeing, Printing, Washing, Finishing, Compacting, Embroidery, Coating/Heat Set, Other)
- LossReasonMaster (Shrinkage, Short meter, Damage, Shade rejection, Testing, Processor claim, Other)
- ReturnReasonMaster (Shade, Quality, Short meter, Wrong design, Rate dispute, Other)
- DefectMaster (Shade variation, stains, holes, misregistration, uneven finish, etc.)
"""
from decimal import Decimal
from django.core.management.base import BaseCommand


PROCESSES = [
    {"code": "DYEING", "name": "Dyeing", "expected_loss_percent": Decimal("5.0"), "tolerance_percent": Decimal("2.0")},
    {"code": "PRINTING", "name": "Printing", "expected_loss_percent": Decimal("4.0"), "tolerance_percent": Decimal("1.5")},
    {"code": "WASHING", "name": "Washing", "expected_loss_percent": Decimal("3.0"), "tolerance_percent": Decimal("1.0")},
    {"code": "FINISHING", "name": "Finishing", "expected_loss_percent": Decimal("2.5"), "tolerance_percent": Decimal("1.0")},
    {"code": "COMPACTING", "name": "Compacting", "expected_loss_percent": Decimal("2.0"), "tolerance_percent": Decimal("1.0")},
    {"code": "EMBROIDERY", "name": "Embroidery", "expected_loss_percent": Decimal("1.0"), "tolerance_percent": Decimal("0.5")},
    {"code": "COATING", "name": "Coating/Heat Set", "expected_loss_percent": Decimal("2.0"), "tolerance_percent": Decimal("1.0")},
    {"code": "OTHER", "name": "Other", "expected_loss_percent": Decimal("0.0"), "tolerance_percent": Decimal("0.0")},
]

LOSS_REASONS = [
    {"code": "SHRINKAGE", "name": "Normal shrinkage/process loss", "description": "Expected dimensional loss during wet processing"},
    {"code": "SHORT_METER", "name": "Short meter", "description": "Shortage received from processor or mill"},
    {"code": "DAMAGE", "name": "Damage/hole/stain", "description": "Physical fabric damage incurred during processing"},
    {"code": "SHADE_REJECT", "name": "Shade rejection", "description": "Off-shade or rejected lot portions"},
    {"code": "SAMPLE_CONSUMPTION", "name": "Testing/sample consumption", "description": "Lab dip, head end, and testing cuts"},
    {"code": "PROCESSOR_CLAIM", "name": "Processor claim/scrap", "description": "Processor liability scrap"},
    {"code": "OTHER_LOSS", "name": "Other approved loss", "description": "Authorized miscellaneous loss"},
]

RETURN_REASONS = [
    {"code": "RET_SHADE", "name": "Shade Mismatch", "description": "Color does not match approved lab dip or swatch"},
    {"code": "RET_QUALITY", "name": "Quality / Construction Failure", "description": "GSM, EPI/PPI, count, or strength failure"},
    {"code": "RET_SHORT_METER", "name": "Short Meterage", "description": "Received meterage significantly below invoice/challan"},
    {"code": "RET_WRONG_DESIGN", "name": "Wrong Design / Strike-off", "description": "Incorrect artwork or strike-off variation"},
    {"code": "RET_RATE_DISPUTE", "name": "Rate Dispute", "description": "Challan/invoice rate differs from contracted PO rate"},
    {"code": "RET_OTHER", "name": "Other", "description": "Other commercial or technical reason"},
]

DEFECTS = [
    {"code": "DEF_SHADE_VAR", "name": "Shade Variation (Listing/Banding)", "category": "shade", "severity": "major"},
    {"code": "DEF_PATCHES", "name": "Dark / Light Patches", "category": "shade", "severity": "major"},
    {"code": "DEF_OIL_STAIN", "name": "Oil / Grease Stain", "category": "stain", "severity": "minor"},
    {"code": "DEF_COLOR_STAIN", "name": "Color / Dye Stain", "category": "stain", "severity": "minor"},
    {"code": "DEF_HOLE", "name": "Tear / Hole / Cut", "category": "hole", "severity": "critical"},
    {"code": "DEF_PIN_HOLE", "name": "Pin Holes / Needle Holes", "category": "hole", "severity": "major"},
    {"code": "DEF_PRINT_MISALIGN", "name": "Printing Misregistration / Out of Fit", "category": "printing defect", "severity": "major"},
    {"code": "DEF_UNEVEN_FINISH", "name": "Uneven Finish / Hand Feel", "category": "finish", "severity": "minor"},
    {"code": "DEF_WIDTH_VAR", "name": "Width Variation (Narrow/Wide)", "category": "other", "severity": "minor"},
    {"code": "DEF_WEAVE_DEFECT", "name": "Weaving Slub / Missing Pick", "category": "other", "severity": "minor"},
]


class Command(BaseCommand):
    help = "Seed Process, Loss Reason, Return Reason, and Defect masters for all tenants."

    def handle(self, *args, **options):
        from apps.accounts.models import Client
        from apps.masters.models import ProcessMaster, LossReasonMaster, ReturnReasonMaster, DefectMaster

        clients = Client.objects.all().only("id", "name")
        p_count = lr_count = rr_count = d_count = 0

        for client in clients:
            for item in PROCESSES:
                _, created = ProcessMaster.objects.get_or_create(
                    client=client,
                    code=item["code"],
                    defaults={
                        "name": item["name"],
                        "expected_loss_pct": item["expected_loss_percent"],
                        "tolerance_pct": item["tolerance_percent"],
                        "is_active": True,
                    },
                )
                if created:
                    p_count += 1

            for item in LOSS_REASONS:
                _, created = LossReasonMaster.objects.get_or_create(
                    client=client,
                    code=item["code"],
                    defaults={
                        "name": item["name"],
                        "description": item["description"],
                        "is_active": True,
                    },
                )
                if created:
                    lr_count += 1

            for item in RETURN_REASONS:
                _, created = ReturnReasonMaster.objects.get_or_create(
                    client=client,
                    code=item["code"],
                    defaults={
                        "name": item["name"],
                        "description": item["description"],
                        "is_active": True,
                    },
                )
                if created:
                    rr_count += 1

            for item in DEFECTS:
                _, created = DefectMaster.objects.get_or_create(
                    client=client,
                    code=item["code"],
                    defaults={
                        "name": item["name"],
                        "category": item["category"],
                        "severity": item["severity"],
                        "is_active": True,
                    },
                )
                if created:
                    d_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully seeded textile masters: {p_count} processes, "
                f"{lr_count} loss reasons, {rr_count} return reasons, {d_count} defects."
            )
        )
