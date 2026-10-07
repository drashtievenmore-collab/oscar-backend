"""
Seed a demo tenant (api.md §13, db.md §14).

Follows the seeder rules of db.md §14.1:

  1. Dates are ISO -- nothing here emits the mock files' ``"27/08/2026"``.
  2. Every row keeps its mock id in ``legacy_id`` so cross-references resolve.
  3. **Derive, do not copy, derived fields.** Stock, balances and document
     totals are produced by posting the underlying rows -- movements, ledger
     entries, lines -- not by writing a snapshot figure.
  4. Seeds in dependency order: tenant -> permissions/roles/users -> masters ->
     accounts -> documents -> movements/ledger -> CRM -> PMS -> HRMS.
  5. Idempotent: every insert is keyed on ``(client_id, legacy_id)``.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.numbering import allocate_number, seed_series_for_client
from apps.core.tenancy import tenant_context


class Command(BaseCommand):
    help = "Create or refresh a demo tenant with representative data."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="sweven", help="Tenant slug.")
        parser.add_argument("--name", default="Sweven Fabricators", help="Tenant name.")
        parser.add_argument(
            "--email", default="admin@sweven.test", help="Administrator email."
        )
        parser.add_argument("--password", default="Sweven@2026", help="Administrator password.")
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete the tenant's existing demo rows before seeding.",
        )

    def handle(self, *args, **options):
        from apps.accounts.models import Client

        slug = options["tenant"]
        client, created = Client.objects.get_or_create(
            slug=slug,
            defaults={
                "name": options["name"],
                "plan": "Enterprise",
                "currency": "INR",
                "fy_start_month": 4,
                "is_demo": True,
                "onboarded_on": timezone.localdate(),
            },
        )
        self.stdout.write(
            self.style.SUCCESS(f"{'Created' if created else 'Using'} tenant {client.name}")
        )

        if options["reset"]:
            self._reset(client)

        with tenant_context(client.id, push_to_db=False):
            with transaction.atomic():
                seed_series_for_client(client)
                admin = self._seed_access(client, options)
                self._seed_company(client)
                accounts = self._seed_accounts(client)
                masters = self._seed_masters(client, admin)
                self._seed_opening_stock(client, masters, admin)
                self._seed_sales(client, masters, accounts, admin)
                self._seed_purchase(client, masters, accounts, admin)
                self._seed_crm(client, admin)
                self._seed_pms(client, admin)
                self._seed_hrms(client, admin)

        self.stdout.write(self.style.SUCCESS("\nDemo data ready."))
        self.stdout.write(f"  Tenant   : {client.name} ({client.slug})")
        self.stdout.write(f"  Sign in  : {options['email']} / {options['password']}")

    # -- reset -------------------------------------------------------------
    def _reset(self, client):
        """Truncate the tenant's rows in reverse dependency order (db.md §14.1)."""
        from apps.core.snapshot import SNAPSHOT_MODELS
        from django.apps import apps as django_apps

        self.stdout.write("Resetting demo data...")
        for label in reversed(SNAPSHOT_MODELS):
            model = django_apps.get_model(label)
            if label == "accounts.User":
                model.objects.filter(client=client, is_superuser=False).delete()
                continue
            if hasattr(model, "client_id"):
                model.objects.filter(client=client).delete()

        from apps.core.models import AuditLog, File, Notification

        AuditLog.objects.filter(client=client).delete()
        Notification.objects.filter(client=client).delete()
        File.objects.filter(client=client).delete()

    # -- §14.1 step 4, in order --------------------------------------------
    def _seed_access(self, client, options):
        from apps.accounts.models import Role, User
        from apps.accounts.permission_catalogue import seed_roles, sync_permissions

        created, updated = sync_permissions()
        self.stdout.write(f"  permissions: +{created} ~{updated}")
        seed_roles(client)

        role_by_code = {role.code: role for role in Role.objects.filter(client=client)}

        admin, was_created = User.objects.get_or_create(
            client=client,
            email=options["email"].lower(),
            defaults={
                "name": "Demo Administrator",
                "role": role_by_code.get("AD"),
                "status": "Active",
                "joined_date": timezone.localdate(),
                "department": "Administration",
            },
        )
        if was_created or not admin.has_usable_password():
            admin.set_password(options["password"])
            admin.save()

        team = [
            ("priya@sweven.test", "Priya Patel", "AC", "Accounts", ["Area Sales Manager"]),
            ("rahul@sweven.test", "Rahul Verma", "SM", "Sales", ["BDE", "Area Sales Manager"]),
            ("neha@sweven.test", "Neha Shah", "SM", "Sales", ["Tele Caller Executive"]),
            ("arjun@sweven.test", "Arjun Mehta", "PU", "Purchase", []),
            ("kiran@sweven.test", "Kiran Rao", "PM", "Projects", ["Technical Lead"]),
            ("divya@sweven.test", "Divya Nair", "HR", "Human Resources", []),
            ("sameer@sweven.test", "Sameer Joshi", "ST", "Stores", ["Sales Support Executive"]),
        ]
        for email, name, role_code, department, crm_roles in team:
            user, made = User.objects.get_or_create(
                client=client,
                email=email,
                defaults={
                    "name": name,
                    "role": role_by_code.get(role_code),
                    "department": department,
                    "status": "Active",
                    "joined_date": timezone.localdate() - timedelta(days=200),
                    "crm_roles": crm_roles,
                },
            )
            if made:
                user.set_password(options["password"])
                user.save()

        self.stdout.write(f"  users: {User.objects.filter(client=client).count()}")
        return admin

    def _seed_company(self, client):
        from apps.core.models import CompanyProfile, Setting
        from apps.core.views import DEFAULT_PREFERENCES, DEFAULT_TAX_SETTINGS

        CompanyProfile.objects.get_or_create(
            client=client,
            defaults={
                "legal_name": "Sweven Fabricators Private Limited",
                "trade_name": "Sweven Fabricators",
                "gstin": "27AABCS1429B1ZX",
                "pan": "AABCS1429B",
                # This is what decides CGST+SGST vs IGST on every document
                # (api.md §5.7) -- read from here, never hardcoded.
                "state": "Maharashtra",
                "state_code": "27",
                "address": {
                    "line1": "Plot 42, MIDC Industrial Area",
                    "line2": "Bhosari",
                    "city": "Pune",
                    "state": "Maharashtra",
                    "pincode": "411026",
                },
                "phone": "+91 20 2712 3456",
                "email": "accounts@sweven.test",
            },
        )
        for key, value in (
            ("preferences", DEFAULT_PREFERENCES),
            ("tax", DEFAULT_TAX_SETTINGS),
        ):
            Setting.objects.get_or_create(client=client, key=key, defaults={"value": value})

    def _seed_accounts(self, client):
        from apps.accounting.models import BankAccount
        from apps.accounting.services import seed_chart_of_accounts, system_account

        seed_chart_of_accounts(client)
        bank, _ = BankAccount.objects.get_or_create(
            client=client,
            legacy_id="bank-1",
            defaults={
                "account": system_account(client.id, "bank"),
                "name": "HDFC Current Account",
                "type": "Bank",
                "account_number": "50200012345678",
                "ifsc": "HDFC0000123",
                "bank_name": "HDFC Bank",
                "branch": "Bhosari",
                "opening_balance": Decimal("2500000.00"),
                "is_default": True,
            },
        )
        BankAccount.objects.get_or_create(
            client=client,
            legacy_id="cash-1",
            defaults={
                "account": system_account(client.id, "cash"),
                "name": "Petty Cash",
                "type": "Cash",
                "opening_balance": Decimal("25000.00"),
            },
        )
        self.stdout.write("  chart of accounts + bank accounts")
        return {"bank": bank}

    def _seed_masters(self, client, admin):
        from apps.masters.models import DEFAULT_FABRICS, Fabric, Item, ItemCategory, Location, Party, Unit

        import re

        def _slug(name):
            cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip()).strip("-").upper()
            return cleaned[:24] or "FABRIC"

        for name in DEFAULT_FABRICS:
            Fabric.objects.get_or_create(
                client=client, name=name, defaults={"code": _slug(name)}
            )

        for code, label in (
            ("Nos", "Numbers"), ("Kg", "Kilogram"), ("Mtr", "Metre"),
            ("Sqft", "Square Feet"), ("Set", "Set"), ("Ltr", "Litre"),
        ):
            Unit.objects.get_or_create(client=client, code=code, defaults={"label": label})

        locations = {}
        for code, name, kind in (
            ("WH-MAIN", "Main Central Hub", "Warehouse"),
            ("WH-SHOP", "Shop Floor Zone A", "Zone"),
            ("WH-TRANSIT", "In Transit", "Transit"),
        ):
            location, _ = Location.objects.get_or_create(
                client=client, code=code, defaults={"name": name, "type": kind}
            )
            locations[code] = location

        categories = {}
        for code, name, kind, hsn in (
            ("CAT-SHEET", "CRCA Sheet", "stock", "7208.10"),
            ("CAT-SECTION", "MS Angle & Channel", "stock", "7216.32"),
            ("CAT-PIPE", "MS Pipe", "stock", "7306.30"),
            ("CAT-FAST", "Fasteners", "stock", "7318.15"),
            ("CAT-MACHINE", "Assembled Machines", "machine", "8479.89"),
            ("CAT-FABRIC", "Grey Fabric", "stock", "5208.52"),
        ):
            category, _ = ItemCategory.objects.get_or_create(
                client=client,
                code=code,
                defaults={"name": name, "kind": kind, "default_hsn_code": hsn,
                          "lead_time_days": 7},
            )
            categories[code] = category

        item_rows = [
            # (legacy, sku, name, category, uom, cost, sell, reorder, weight_item, theoretical)
            ("itm-1", "STL-2MM-CRCA", "CRCA Sheet 2mm", "CAT-SHEET", "Kg",
             "62.5000", "78.0000", "500", True, "47.1000"),
            ("itm-2", "STL-3MM-CRCA", "CRCA Sheet 3mm", "CAT-SHEET", "Kg",
             "61.0000", "76.5000", "400", True, "70.6500"),
            ("itm-3", "MS-ANG-50", "MS Angle 50x50x5", "CAT-SECTION", "Mtr",
             "310.0000", "395.0000", "120", True, "3.8000"),
            ("itm-4", "MS-CHN-100", "MS Channel 100x50", "CAT-SECTION", "Mtr",
             "640.0000", "790.0000", "80", True, "9.5600"),
            ("itm-5", "MS-PIPE-50", "MS Pipe 50NB", "CAT-PIPE", "Mtr",
             "285.0000", "360.0000", "150", True, "5.4100"),
            ("itm-6", "FAS-M10-HEX", "Hex Bolt M10x50", "CAT-FAST", "Nos",
             "12.0000", "19.0000", "2000", False, None),
            ("itm-7", "FAS-M12-NUT", "Hex Nut M12", "CAT-FAST", "Nos",
             "6.5000", "11.0000", "3000", False, None),
        ]
        items = {}
        for (legacy, sku, name, category_code, uom, cost, sell, reorder,
             is_weight, theoretical) in item_rows:
            item, _ = Item.objects.get_or_create(
                client=client,
                sku=sku,
                defaults={
                    "legacy_id": legacy,
                    "name": name,
                    "category": categories[category_code],
                    "item_kind": "Standalone",
                    "uom": uom,
                    "hsn_code": categories[category_code].default_hsn_code,
                    "cost_price": Decimal(cost),
                    "selling_price": Decimal(sell),
                    "reorder_level": Decimal(reorder),
                    "is_weight_item": is_weight,
                    "theoretical_weight": Decimal(theoretical) if theoretical else None,
                    "tolerance_pct": Decimal("2"),
                    "default_location": locations["WH-MAIN"],
                    "created_by": admin,
                },
            )
            items[legacy] = item

        # Fabric-kind items for the PO grey-fabric dropdown (meter-based).
        def _sku(name):
            cleaned = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").upper()
            return f"FAB-{cleaned}"[:40]

        for name in DEFAULT_FABRICS:
            item, _ = Item.objects.get_or_create(
                client=client,
                sku=_sku(name),
                defaults={
                    "name": f"{name} Grey Fabric",
                    "category": categories["CAT-FABRIC"],
                    "item_kind": "Fabric",
                    "uom": "Mtr",
                    "hsn_code": "5208.52",
                    "cost_price": Decimal("0"),
                    "selling_price": Decimal("0"),
                    "reorder_level": Decimal("0"),
                    "fabric_quality": name,
                    "default_location": locations["WH-MAIN"],
                    "created_by": admin,
                },
            )
            items[_sku(name)] = item

        # A machine with a BOM, so the explosion endpoint has something to say.
        machine, made = Item.objects.get_or_create(
            client=client,
            sku="MCH-CONV-01",
            defaults={
            
                "legacy_id": "itm-100",
                "name": "Belt Conveyor 6m",
                "category": categories["CAT-MACHINE"],
                "item_kind": "Machine",
                "uom": "Nos",
                "hsn_code": "8479.89",
                "cost_price": Decimal("184000.0000"),
                "selling_price": Decimal("242000.0000"),
                "reorder_level": Decimal("2"),
                "tracking_mode": "Serial",
                "default_location": locations["WH-MAIN"],
                "created_by": admin,
            },
        )
        items["itm-100"] = machine
        if made:
            from apps.masters.models import ItemPart

            for part_legacy, qty in (("itm-3", "24"), ("itm-5", "12"), ("itm-6", "96")):
                ItemPart.objects.get_or_create(
                    client=client,
                    parent_item=machine,
                    part_item=items[part_legacy],
                    defaults={"required_qty": Decimal(qty)},
                )

        party_rows = [
            # The demo sales chain below runs to ~6.4 lakh of exposure, so this
            # limit is set above it deliberately: the seeder must exercise the
            # happy path, not trip the credit-limit guard (api.md §4.1).
            ("cust-1", "Acme Engineering Works", "Customer", "Gujarat",
             "24AABCA1234A1Z5", "1500000"),
            ("cust-2", "Deccan Auto Components", "Customer", "Maharashtra",
             "27AACCD5678K1Z9", "750000"),
            ("cust-3", "Coastal Marine Fabricators", "Customer", "Goa",
             "30AAGCC9012L1Z3", "300000"),
            ("vend-1", "Bharat Steel Traders", "Vendor", "Maharashtra",
             "27AABCB2345C1Z1", None),
            ("vend-2", "Precision Fasteners Co", "Vendor", "Tamil Nadu",
             "33AAECP6789M1Z7", None),
        ]
        parties = {}
        for legacy, name, kind, state, gstin, credit in party_rows:
            party, _ = Party.objects.get_or_create(
                client=client,
                legacy_id=legacy,
                defaults={
                    "code": allocate_number(
                        client, "CUST" if kind == "Customer" else "VEND"
                    ),
                    "type": kind,
                    "name": name,
                    "phone": "98765" + legacy[-5:].rjust(5, "0"),
                    "email": f"{legacy}@example.test",
                    "gst_treatment": "Registered Business",
                    "gstin": gstin,
                    "place_of_supply": state,
                    "credit_limit": Decimal(credit) if credit else None,
                    "payment_terms": "Net 30",
                    "billing_address": {
                        "line1": f"{name} Industrial Estate",
                        "city": "Pune" if state == "Maharashtra" else "Ahmedabad",
                        "state": state,
                        "pincode": "411026",
                    },
                    "created_by": admin,
                },
            )
            parties[legacy] = party

        self.stdout.write(
            f"  masters: {len(items)} items, {len(parties)} parties, "
            f"{len(locations)} locations"
        )
        return {
            "items": items,
            "parties": parties,
            "locations": locations,
            "categories": categories,
        }

    def _seed_opening_stock(self, client, masters, admin):
        """db.md §14.1 rule 3 -- a balance with no movement behind it gets an
        opening-balance movement, so the number has a cause."""
        from apps.inventory import services as stock
        from apps.inventory.models import StockMovement

        location = masters["locations"]["WH-MAIN"]
        opening = {
            "itm-1": "1800", "itm-2": "1200", "itm-3": "400", "itm-4": "260",
            "itm-5": "520", "itm-6": "5200", "itm-7": "6400", "itm-100": "3",
        }
        posted = 0
        for legacy, quantity in opening.items():
            item = masters["items"][legacy]
            if StockMovement.objects.filter(
                client=client, item=item, type="ADJUSTMENT",
                notes="Opening stock (demo seed)",
            ).exists():
                continue
            stock.post_movement(
                client_id=client.id,
                item=item,
                location=location,
                type="ADJUSTMENT",
                quantity=Decimal(quantity),
                unit_cost=item.cost_price,
                notes="Opening stock (demo seed)",
                user=admin,
                movement_date=timezone.localdate() - timedelta(days=60),
            )
            posted += 1

        # Serials for the serial-tracked machine.
        from apps.masters.models import ItemSerial

        machine = masters["items"]["itm-100"]
        for index in range(1, 4):
            ItemSerial.objects.get_or_create(
                client=client,
                item=machine,
                serial_no=f"SWV-CONV-2026-{index:04d}",
                defaults={"location": location, "status": "available"},
            )
        self.stdout.write(f"  opening stock: {posted} movement(s)")

    def _seed_sales(self, client, masters, accounts, admin):
        """A quotation -> order -> challan -> invoice -> payment chain, posted
        through the real services so stock and the ledger are derived."""
        from apps.sales import services as sales_services
        from apps.sales.models import (
            Quotation,
            QuotationLine,
            SalesInvoice,
            SalesInvoiceLine,
            SalesOrder,
            SalesOrderLine,
        )

        if SalesInvoice.objects.filter(client=client, legacy_id="inv-demo-1").exists():
            self.stdout.write("  sales: already seeded")
            return

        customer = masters["parties"]["cust-1"]
        today = timezone.localdate()

        quotation = Quotation(
            client=client,
            legacy_id="qt-demo-1",
            party=customer,
            doc_date=today - timedelta(days=21),
            valid_until=today + timedelta(days=9),
            status="Accepted",
            subject="Conveyor and structural steel package",
            created_by=admin,
        )
        quotation.freeze_party_snapshot()
        quotation.quotation_number = allocate_number(client, "QT", quotation.doc_date)
        quotation.save()

        quote_lines = [
            ("itm-100", "1", "242000.0000", "0", "18"),
            ("itm-3", "60", "395.0000", "5", "18"),
            ("itm-6", "400", "19.0000", "0", "18"),
        ]
        for index, (legacy, qty, rate, discount, tax) in enumerate(quote_lines, start=1):
            item = masters["items"][legacy]
            line = QuotationLine(
                client=client, quotation=quotation, line_no=index, item=item,
                qty=Decimal(qty), rate=Decimal(rate),
                discount_pct=Decimal(discount), tax_pct=Decimal(tax),
            )
            line.freeze_item_snapshot()
            line.save()
        sales_services.recalculate_document(quotation)

        order = SalesOrder(
            client=client,
            legacy_id="so-demo-1",
            party=customer,
            party_name=quotation.party_name,
            party_gstin=quotation.party_gstin,
            billing_address=quotation.billing_address,
            shipping_address=quotation.shipping_address,
            place_of_supply=quotation.place_of_supply,
            doc_date=today - timedelta(days=14),
            delivery_date=today + timedelta(days=7),
            quotation=quotation,
            stage="Confirmed",
            created_by=admin,
        )
        order.order_number = allocate_number(client, "SO", order.doc_date)
        order.save()

        for line in quotation.line_items.order_by("line_no"):
            SalesOrderLine.objects.create(
                client=client, sales_order=order, line_no=line.line_no,
                item=line.item, sku=line.sku, item_name=line.item_name,
                hsn_code=line.hsn_code, uom=line.uom, qty=line.qty, rate=line.rate,
                discount_pct=line.discount_pct, tax_pct=line.tax_pct,
            )
        sales_services.recalculate_document(order)
        quotation.status = "Converted"
        quotation.save(update_fields=["status"])

        invoice = SalesInvoice(
            client=client,
            legacy_id="inv-demo-1",
            party=customer,
            party_name=order.party_name,
            party_gstin=order.party_gstin,
            billing_address=order.billing_address,
            shipping_address=order.shipping_address,
            place_of_supply=order.place_of_supply,
            doc_date=today - timedelta(days=7),
            sales_order=order,
            location=masters["locations"]["WH-MAIN"],
            status="Draft",
            created_by=admin,
        )
        invoice.save()

        from apps.inventory import services as stock

        for line in order.line_items.order_by("line_no"):
            invoice_line = SalesInvoiceLine.objects.create(
                client=client, sales_invoice=invoice, line_no=line.line_no,
                item=line.item, sku=line.sku, item_name=line.item_name,
                hsn_code=line.hsn_code, uom=line.uom, qty=line.qty, rate=line.rate,
                discount_pct=line.discount_pct, tax_pct=line.tax_pct,
                sales_order_line=line,
            )
            # A serial-tracked line must carry a serial selection matching its
            # quantity (api.md §4.2) -- the seeder picks one, as a user would.
            if line.item and line.item.tracking_mode == "Serial":
                available = stock.resolve_serials(
                    client.id,
                    line.item_id,
                    list(
                        line.item.serials.filter(status="available")
                        .order_by("serial_no")
                        .values_list("serial_no", flat=True)[: int(line.qty)]
                    ),
                )
                stock.link_line_serials(
                    client.id, "sales_invoice_lines", invoice_line.id, available
                )
        sales_services.recalculate_document(invoice)

        # Finalize through the real service: allocates the number, posts SALE
        # movements and the Dr Debtors / Cr Sales entry.
        sales_services.finalize_invoice(invoice, user=admin)
        invoice.refresh_from_db()

        sales_services.record_payment_in(
            client=client,
            party=customer,
            amount=Decimal("150000.00"),
            payment_date=today - timedelta(days=2),
            mode="Bank",
            bank_account=accounts["bank"],
            reference_number="NEFT-DEMO-0001",
            allocations=[{"invoiceId": invoice.id, "amount": Decimal("150000.00")}],
            user=admin,
        )

        self.stdout.write(
            f"  sales: {quotation.quotation_number} -> {order.order_number} "
            f"-> {invoice.invoice_number} (part paid)"
        )

    def _seed_purchase(self, client, masters, accounts, admin):
        """A bill received against a weighbridge, so the weight-variance rule
        (api.md §6.3) has a worked example."""
        from apps.purchase import services as purchase_services
        from apps.purchase.models import PurchaseBill, PurchaseBillLine

        if PurchaseBill.objects.filter(client=client, legacy_id="bill-demo-1").exists():
            self.stdout.write("  purchase: already seeded")
            return

        vendor = masters["parties"]["vend-1"]
        today = timezone.localdate()

        bill = PurchaseBill(
            client=client,
            legacy_id="bill-demo-1",
            party=vendor,
            doc_date=today - timedelta(days=10),
            due_date=today + timedelta(days=20),
            vendor_bill_number="BST/2026/1187",
            location=masters["locations"]["WH-MAIN"],
            status="Draft",
            created_by=admin,
        )
        bill.freeze_party_snapshot()
        bill.bill_number = allocate_number(client, "BILL", bill.doc_date)
        bill.save()

        for index, (legacy, qty, rate) in enumerate(
            (("itm-1", "400", "62.5000"), ("itm-3", "120", "310.0000")), start=1
        ):
            item = masters["items"][legacy]
            line = PurchaseBillLine(
                client=client, purchase_bill=bill, line_no=index, item=item,
                qty=Decimal(qty), rate=Decimal(rate), tax_pct=Decimal("18"),
                is_weight_item=item.is_weight_item,
            )
            line.freeze_item_snapshot()
            line.save()

        # Freight is applied with the recompute, never before it: the header
        # total check (db.md §3.1) must hold on every statement.
        bill.freight_charges = Decimal("4500.00")
        purchase_services.recalculate_document(bill)

        # The weighbridge reads slightly under on the sheet -- inside the 2%
        # tolerance, so QC stays Approved and the bill is revalued at the
        # received weight.
        result = purchase_services.receive_bill_goods(
            bill,
            lines_payload=[
                {"lineIndex": 0, "receivedQty": "400", "receivedWeight": "18720.000"},
                {"lineIndex": 1, "receivedQty": "120", "receivedWeight": "456.000"},
            ],
            qc_status="Approved",
            user=admin,
        )
        self.stdout.write(
            f"  purchase: {bill.bill_number} received, QC {result['qcStatus']}"
        )

    def _seed_crm(self, client, admin):
        from apps.accounts.models import User
        from apps.crm.models import Lead, Stage, StageTask
        from apps.crm.services import seed_crm_configuration

        seed_crm_configuration(client)

        stages = {stage.name: stage for stage in Stage.objects.filter(client=client)}
        templates = [
            ("New Lead", "Introductory Call", "Tele Caller Executive", 1),
            ("Details Collected", "Send Company Profile", "BDE", 1),
            ("Quotation Shared", "Quotation Follow-up", "BDE", 2),
            ("Demo Pending", "Schedule Demo", "Area Sales Manager", 2),
            ("Negotiation", "Negotiation Review", "Area Sales Manager", 1),
        ]
        for stage_name, title, role, offset in templates:
            stage = stages.get(stage_name)
            if stage is None:
                continue
            StageTask.objects.get_or_create(
                client=client,
                stage=stage,
                title=title,
                defaults={
                    "assignee_role": role,
                    "offset_days": offset,
                    "priority": "Medium",
                    "auto_create": True,
                },
            )

        owner = User.objects.filter(client=client, email="rahul@sweven.test").first()
        lead_rows = [
            ("lead-1", "Sunil Kulkarni", "Vertex Packaging", "New Lead", "Pune", "450000"),
            ("lead-2", "Meera Iyer", "Sterling Foods", "Quotation Shared", "Nashik", "820000"),
            ("lead-3", "Rakesh Gupta", "Orion Logistics", "Demo Pending", "Mumbai", "310000"),
        ]
        created = 0
        for legacy, name, company, stage_name, city, amount in lead_rows:
            if Lead.objects.filter(client=client, legacy_id=legacy).exists():
                continue
            lead = Lead.objects.create(
                client=client,
                legacy_id=legacy,
                lead_number=allocate_number(client, "LEAD"),
                name=name,
                company=company,
                phone="99887" + legacy[-5:].rjust(5, "0"),
                email=f"{legacy}@example.test",
                stage=stages.get(stage_name) or stages["New Lead"],
                owner=owner,
                city=city,
                state="Maharashtra",
                country="India",
                amount=Decimal(amount),
                created_by=admin,
            )
            # Runs the real automation, so the generated tasks are real.
            from apps.crm.services import run_stage_automation

            run_stage_automation(lead, lead.stage, user=admin)
            created += 1

        self.stdout.write(f"  crm: {created} lead(s) with stage automation applied")

    def _seed_pms(self, client, admin):
        from apps.accounts.models import User
        from apps.pms.models import Department, Project, StageConfig
        from apps.pms.services import get_settings
        from apps.pms.views import apply_stage_template, create_project_from_order
        from apps.sales.models import SalesOrder

        get_settings(client.id)

        departments = {}
        for name, color, capacity in (
            ("Design", "#1f6bff", 18),
            ("Production", "#6d28d9", 24),
            ("Quality", "#0e7490", 12),
            ("Packaging", "#7c3aed", 10),
            ("Installation", "#1d4ed8", 8),
        ):
            department, _ = Department.objects.get_or_create(
                client=client, name=name,
                defaults={"color": color, "capacity": capacity},
            )
            departments[name] = department

        configs = []
        for sequence, (name, department, duration, needs_doc, needs_approval) in enumerate(
            (
                ("Design & Drawing", "Design", "3", True, True),
                ("Fabrication", "Production", "7", False, False),
                ("Quality Inspection", "Quality", "2", True, False),
                ("Packaging", "Packaging", "1", False, False),
                ("Installation", "Installation", "2", False, True),
            ),
            start=1,
        ):
            config, _ = StageConfig.objects.get_or_create(
                client=client,
                name=name,
                defaults={
                    "sequence": sequence,
                    "department": departments[department],
                    "default_duration": Decimal(duration),
                    "duration_unit": "Days",
                    "required_document": needs_doc,
                    "required_approval": needs_approval,
                },
            )
            configs.append(config)

        order = SalesOrder.objects.filter(
            client=client, legacy_id="so-demo-1", pms_project__isnull=True
        ).first()
        if order is not None:
            manager = User.objects.filter(client=client, email="kiran@sweven.test").first()
            project = create_project_from_order(
                order,
                project_manager_id=manager.id if manager else None,
                priority="High",
                stage_config_ids=[config.id for config in configs],
                user=admin,
            )
            self.stdout.write(f"  pms: project {project.code} with {len(configs)} stages")
        else:
            self.stdout.write("  pms: configuration ready")

    def _seed_hrms(self, client, admin):
        from apps.hrms.models import (
            Department,
            Designation,
            Employee,
            LeaveType,
            Location,
            WorkingDay,
        )

        location, _ = Location.objects.get_or_create(
            client=client, name="Pune Works",
            defaults={"address": {"city": "Pune", "state": "Maharashtra"}},
        )
        departments = {}
        for name in ("Production", "Quality", "Sales", "Accounts", "Human Resources"):
            department, _ = Department.objects.get_or_create(client=client, name=name)
            departments[name] = department

        designations = {}
        for name, level in (
            ("Works Manager", 3), ("Senior Fabricator", 2), ("QC Inspector", 2),
            ("Sales Executive", 1), ("Accounts Executive", 1),
        ):
            designation, _ = Designation.objects.get_or_create(
                client=client, name=name, defaults={"level": level}
            )
            designations[name] = designation

        for weekday in range(7):
            WorkingDay.objects.get_or_create(
                client=client,
                weekday=weekday,
                location=None,
                defaults={"is_working": weekday < 6},  # six-day week
            )

        for name, entitlement, accrual, paid in (
            ("Casual Leave", "12", "Yearly", True),
            ("Sick Leave", "8", "Yearly", True),
            ("Earned Leave", "18", "Monthly", True),
            ("Loss of Pay", "0", "None", False),
        ):
            LeaveType.objects.get_or_create(
                client=client,
                name=name,
                defaults={
                    "annual_entitlement": Decimal(entitlement),
                    "accrual": accrual,
                    "is_paid": paid,
                    "carry_forward_cap": Decimal("10") if accrual != "None" else None,
                },
            )

        employee_rows = [
            ("emp-1", "Sanjay Deshmukh", "Production", "Works Manager", "68000"),
            ("emp-2", "Farhan Qureshi", "Production", "Senior Fabricator", "34000"),
            ("emp-3", "Anita Kulkarni", "Quality", "QC Inspector", "38000"),
            ("emp-4", "Rohit Pawar", "Sales", "Sales Executive", "32000"),
            ("emp-5", "Sneha Raut", "Accounts", "Accounts Executive", "36000"),
        ]
        created = 0
        for legacy, name, department, designation, salary in employee_rows:
            if Employee.objects.filter(client=client, legacy_id=legacy).exists():
                continue
            Employee.objects.create(
                client=client,
                legacy_id=legacy,
                employee_code=allocate_number(client, "EMP"),
                name=name,
                email=f"{legacy}@sweven.test",
                phone="90210" + legacy[-5:].rjust(5, "0"),
                department=departments[department],
                designation=designations[designation],
                location=location,
                joining_date=timezone.localdate() - timedelta(days=400),
                employment_type="Full-time",
                standard_salary=Decimal(salary),
                status="Active",
                created_by=admin,
            )
            created += 1

        self.stdout.write(f"  hrms: {created} employee(s), leave types, working days")
