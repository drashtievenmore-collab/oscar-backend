from datetime import date
from decimal import Decimal
import uuid
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounting import services as ledger
from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.models import AlertIncident, AlertSetting
from apps.core import alert_services
from apps.inventory import services as stock
from apps.inventory.models import LotTaka, StockBalance, StockMovement
from apps.masters.models import FabricQuality, Item, ItemCategory, Location, Party, Unit
from apps.sales.models import SalesOrder, SalesOrderLine

D = Decimal


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


class CombinedGreyStockTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(slug=uid("tenant"), name="Textile Mill Tenant")
        self.user = User.objects.create_user(
            email=f"{uid('user')}@textile.com",
            password="testpassword",
            client=self.client_obj,
            is_superuser=True,
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

        ledger.seed_chart_of_accounts(self.client_obj)

        self.uom_mtr = Unit.objects.create(
            client=self.client_obj, code="Mtr", label="Meter", uom_type="Length", is_base_unit=True
        )
        self.uom_kg = Unit.objects.create(
            client=self.client_obj, code="Kg", label="Kilogram", uom_type="Weight", is_base_unit=True
        )

        self.cat_fabric = ItemCategory.objects.create(client=self.client_obj, code=uid("CAT-GRY"), name="Grey Fabric")
        self.cat_finished = ItemCategory.objects.create(client=self.client_obj, code=uid("CAT-FIN"), name="Finished Fabric")

        # Godowns
        self.godown_main = Location.objects.create(
            client=self.client_obj, code="GD-MAIN", name="Main Mill Godown", type="Godown", is_active=True
        )
        self.godown_sec = Location.objects.create(
            client=self.client_obj, code="GD-SEC", name="Secondary Godown", type="Godown", is_active=True
        )
        self.qc_loc = Location.objects.create(
            client=self.client_obj, code="SYS-QC-HOLD", name="QC Hold Location", type="Godown", is_active=True
        )
        self.reprocess_loc = Location.objects.create(
            client=self.client_obj, code="SYS-REPROCESS", name="Reprocess Location", type="Godown", is_active=True
        )
        self.reject_loc = Location.objects.create(
            client=self.client_obj, code="SYS-REJECT", name="Scrap / Reject Location", type="Godown", is_active=True
        )
        self.sample_loc = Location.objects.create(
            client=self.client_obj, code="SYS-SAMPLE-ROOM", name="Sample Room", type="Godown", is_active=True
        )

        # External Processor / Vendor
        self.processor_a = Party.objects.create(
            client=self.client_obj, code=uid("PROC-A"), type="Vendor", name="Apex Dyeing Mills"
        )
        self.wip_loc_a = Location.objects.create(
            client=self.client_obj,
            code=f"SYS-WIP-{self.processor_a.id}",
            name=f"WIP - {self.processor_a.name}",
            type="Godown",
            is_active=True,
        )

        # Items
        self.grey_quality_1 = FabricQuality.objects.create(
            client=self.client_obj, code=uid("Q-COT"), name="Cotton 60x60 Cambric"
        )
        self.grey_quality_2 = FabricQuality.objects.create(
            client=self.client_obj, code=uid("Q-RAY"), name="Rayon 30s Twill"
        )

        self.item_grey_1 = Item.objects.create(
            client=self.client_obj,
            sku=uid("GRY-COT"),
            name="Grey Cotton 60x60 Cambric",
            category=self.cat_fabric,
            fabric_quality=self.grey_quality_1.name,
            item_kind="Fabric",
            tracking_type="TAKA",
            cost_price=D("45.00"),
            uom="Mtr",
            reorder_level=D("1000.00"),
        )
        self.item_grey_2 = Item.objects.create(
            client=self.client_obj,
            sku=uid("GRY-RAY"),
            name="Grey Rayon 30s Twill",
            category=self.cat_fabric,
            fabric_quality=self.grey_quality_2.name,
            item_kind="Fabric",
            tracking_type="TAKA",
            cost_price=D("55.00"),
            uom="Mtr",
            reorder_level=D("1500.00"),
        )
        self.item_finished = Item.objects.create(
            client=self.client_obj,
            sku=uid("FIN-COT"),
            name="Finished Dyed Navy Cotton 60x60",
            category=self.cat_finished,
            fabric_quality="Finished Cotton",
            item_kind="Fabric",
            tracking_type="TAKA",
            cost_price=D("85.00"),
            uom="Mtr",
            reorder_level=D("2000.00"),
        )
        self.item_yarn_kg = Item.objects.create(
            client=self.client_obj,
            sku=uid("YRN-KG"),
            name="Cotton Yarn 40s (Cone)",
            category=self.cat_fabric,
            item_kind="Fabric",
            cost_price=D("250.00"),
            uom="Kg",
        )

    def _post_opening(self, item, location, qty, taka_no="1"):
        lot = LotTaka.objects.create(
            client=self.client_obj,
            taka_id=uid("TAK"),
            lot_number=uid("LOT"),
            taka_number=taka_no,
            item=item,
            location=location,
            received_quantity=D(qty),
            available_quantity=D("0"),
            status="Available",
            uom=item.uom,
            rate=item.cost_price,
        )
        stock.post_movement(
            client_id=self.client_obj.id,
            item=item,
            location=location,
            type="INWARD",
            quantity=D(qty),
            lot_taka=lot,
            notes="Opening stock",
            reference_type="GoodsReceipt",
            reference_id=uuid.uuid4(),
            reference_number=uid("GRN"),
            user=self.user,
        )
        lot.refresh_from_db()
        return lot

    def test_combined_grey_stock_calculation_and_multi_quality_sum(self):
        """Authoritative calculation sums available grey stock across qualities in linear meters."""
        self._post_opening(self.item_grey_1, self.godown_main, "5000.00", taka_no="1")
        self._post_opening(self.item_grey_1, self.godown_sec, "3000.00", taka_no="2")
        self._post_opening(self.item_grey_2, self.godown_main, "4000.00", taka_no="3")

        calc = stock.calculate_combined_grey_stock(self.client_obj.id)
        self.assertEqual(calc["physical_grey_stock"], D("12000.0000"))
        self.assertEqual(calc["reserved_grey_stock"], D("0.0000"))
        self.assertEqual(calc["available_grey_stock"], D("12000.0000"))
        self.assertEqual(calc["external_wip"], D("0.0000"))
        self.assertEqual(calc["qualities_count"], 2)
        self.assertEqual(calc["takas_count"], 3)
        self.assertEqual(calc["uom"], "Mtr")

    def test_exclusion_of_finished_qc_hold_reprocess_reject(self):
        """Finished stock, QC hold, reprocess, and rejected stock are excluded from grey stock."""
        # Grey stock at main godown: 6000 Mtr
        self._post_opening(self.item_grey_1, self.godown_main, "6000.00", taka_no="1")

        # Finished stock at main godown: 4000 Mtr (MUST BE EXCLUDED)
        self._post_opening(self.item_finished, self.godown_main, "4000.00", taka_no="2")

        # Grey fabric at QC hold: 1500 Mtr (MUST BE EXCLUDED)
        self._post_opening(self.item_grey_1, self.qc_loc, "1500.00", taka_no="3")

        # Grey fabric at Reprocess: 800 Mtr (MUST BE EXCLUDED)
        self._post_opening(self.item_grey_1, self.reprocess_loc, "800.00", taka_no="4")

        # Grey fabric at Scrap/Reject: 500 Mtr (MUST BE EXCLUDED)
        self._post_opening(self.item_grey_1, self.reject_loc, "500.00", taka_no="5")

        calc = stock.calculate_combined_grey_stock(self.client_obj.id)
        self.assertEqual(calc["physical_grey_stock"], D("6000.0000"))
        self.assertEqual(calc["available_grey_stock"], D("6000.0000"))
        self.assertEqual(calc["takas_count"], 1)

    def test_no_double_counting_across_taka_and_balance(self):
        """Taka registered and loose balance on hand are partitioned without double-counting."""
        item_loose = Item.objects.create(
            client=self.client_obj,
            sku=uid("GRY-LOOSE"),
            name="Grey Sheeting (Untracked)",
            category=self.cat_fabric,
            fabric_quality="Sheeting 40s",
            item_kind="Fabric",
            tracking_type="NONE",
            cost_price=D("35.00"),
            uom="Mtr",
        )
        # 1. Opening taka: 3000 Mtr
        self._post_opening(self.item_grey_1, self.godown_main, "3000.00", taka_no="1")

        # 2. Additional loose balance: 1000 Mtr without lot_taka on untracked item
        stock.post_movement(
            client_id=self.client_obj.id,
            item=item_loose,
            location=self.godown_main,
            type="INWARD",
            quantity=D("1000.00"),
            notes="Loose piece",
            reference_type="GoodsReceipt",
            reference_id=uuid.uuid4(),
            reference_number=uid("GRN"),
            user=self.user,
        )

        calc = stock.calculate_combined_grey_stock(self.client_obj.id)
        # Total physical = 3000 (taka) + 1000 (loose) = 4000
        self.assertEqual(calc["physical_grey_stock"], D("4000.0000"))
        self.assertEqual(calc["available_grey_stock"], D("4000.0000"))
        self.assertEqual(calc["takas_count"], 1)

    def test_incompatible_uom_safely_excluded_from_meter_sum(self):
        """Items with incompatible UOM (Weight/Kg) are excluded from length meter sum."""
        self._post_opening(self.item_grey_1, self.godown_main, "5000.00", taka_no="1")
        # Add yarn in Kg
        stock.post_movement(
            client_id=self.client_obj.id,
            item=self.item_yarn_kg,
            location=self.godown_main,
            type="INWARD",
            quantity=D("500.00"),
            notes="Yarn cones in Kg",
            reference_type="GoodsReceipt",
            reference_id=uuid.uuid4(),
            reference_number=uid("GRN"),
            user=self.user,
        )

        calc = stock.calculate_combined_grey_stock(self.client_obj.id)
        self.assertEqual(calc["physical_grey_stock"], D("5000.0000"))
        self.assertEqual(calc["available_grey_stock"], D("5000.0000"))
        incompatible = [x["sku"] for x in calc["incompatible_items"]]
        self.assertIn(self.item_yarn_kg.sku, incompatible)

    def test_sales_reservation_reduces_available_without_physical_deduction(self):
        """Sales reservations reduce Available-To-Promise without physically moving stock."""
        lot = self._post_opening(self.item_grey_1, self.godown_main, "10000.00", taka_no="1")

        # Customer Order reserving 3000 Mtr
        customer = Party.objects.create(client=self.client_obj, code=uid("CUST"), type="Customer", name="Buyer Corp")
        so = SalesOrder.objects.create(
            client=self.client_obj,
            order_number=uid("SO"),
            party=customer,
            stage="Confirmed",
            doc_date=date.today(),
        )
        SalesOrderLine.objects.create(
            client=self.client_obj,
            sales_order=so,
            item=self.item_grey_1,
            line_no=1,
            qty=D("3000.00"),
            rate=D("60.00"),
        )

        calc = stock.calculate_combined_grey_stock(self.client_obj.id)
        # Physical remains 10000.00 Mtr
        self.assertEqual(calc["physical_grey_stock"], D("10000.0000"))
        # Reserved is 3000.00 Mtr
        self.assertEqual(calc["reserved_grey_stock"], D("3000.0000"))
        # Available is 10000 - 3000 = 7000.00 Mtr
        self.assertEqual(calc["available_grey_stock"], D("7000.0000"))

    def test_job_work_outward_and_inward_traceability(self):
        """Outward JWO transfers from grey stock to external WIP; Inward returns to QC Hold."""
        lot = self._post_opening(self.item_grey_1, self.godown_main, "8000.00", taka_no="1")

        # 1. Send 5000 Mtr to Dyeing Mill processor (External WIP) using relocate_lot
        stock.relocate_lot(
            client_id=self.client_obj.id,
            lot=lot.id,
            quantity=D("5000.00"),
            to_location_id=self.wip_loc_a.id,
            out_type="TRANSFER_OUT",
            in_type="TRANSFER_IN",
            split_suffix="JWO",
            reference_type="JWOOutward",
            reference_id=uuid.uuid4(),
            reference_number=uid("JWO"),
            notes="Outward Challan to Apex Dyeing",
            user=self.user,
        )

        calc = stock.calculate_combined_grey_stock(self.client_obj.id)
        # Godown has 3000 Mtr remaining
        self.assertEqual(calc["physical_grey_stock"], D("3000.0000"))
        self.assertEqual(calc["available_grey_stock"], D("3000.0000"))
        # External WIP has 5000 Mtr
        self.assertEqual(calc["external_wip"], D("5000.0000"))
        self.assertEqual(len(calc["wip_by_party"]), 1)
        self.assertEqual(calc["wip_by_party"][0]["party"], self.processor_a.name)
        self.assertEqual(calc["wip_by_party"][0]["quantity"], D("5000.0000"))

        # Find the moved taka at WIP location
        wip_taka = LotTaka.objects.filter(client=self.client_obj, location=self.wip_loc_a).first()
        self.assertIsNotNone(wip_taka)

        # 2. Inward 4800 Mtr from processor (with 200 Mtr loss) into SYS-QC-HOLD
        stock.relocate_lot(
            client_id=self.client_obj.id,
            lot=wip_taka.id,
            quantity=D("4800.00"),
            to_location_id=self.qc_loc.id,
            out_type="TRANSFER_OUT",
            in_type="TRANSFER_IN",
            split_suffix="JWI",
            reference_type="JWOInward",
            reference_id=uuid.uuid4(),
            reference_number=uid("JWI"),
            notes="Inward received at QC Hold",
            user=self.user,
        )

        calc_after_inward = stock.calculate_combined_grey_stock(self.client_obj.id)
        # WIP at processor reduced to 200 Mtr remaining
        self.assertEqual(calc_after_inward["external_wip"], D("200.0000"))
        # Material at QC Hold is NOT counted in company available grey stock
        self.assertEqual(calc_after_inward["available_grey_stock"], D("3000.0000"))

    def test_combined_grey_stock_alert_lifecycle(self):
        """Alert triggers when available < threshold, updates in place, and auto-resolves on recovery."""
        # Configure combined threshold at 10,000 Mtr
        stock.set_combined_grey_stock_setting(
            client_id=self.client_obj.id,
            threshold=D("10000.00"),
            is_enabled=True,
            user=self.user,
        )

        # 1. Stock is currently 0 Mtr -> Breach threshold
        res = stock.evaluate_combined_grey_stock_alert(self.client_obj.id)
        incident = res["incident"]
        self.assertIsNotNone(incident)
        self.assertEqual(incident.status, "Active")
        self.assertEqual(incident.severity, "Critical")
        self.assertIn("10000", incident.message)

        # 2. Post 7000 Mtr -> Still below 10,000 Mtr threshold (> 50%, shortage = 3000 Mtr)
        lot = self._post_opening(self.item_grey_1, self.godown_main, "7000.00", taka_no="1")
        # Inward movement automatically triggered alert evaluation in post_movement
        incident.refresh_from_db()
        self.assertEqual(incident.status, "Active")
        self.assertEqual(incident.severity, "Warning")  # available > 50% threshold
        self.assertIn("3000", incident.message)

        # 3. Ensure no duplicate active incidents were created
        active_incidents_count = AlertIncident.objects.filter(
            client_id=self.client_obj.id,
            alert_key="combined_grey_stock_low",
            status="Active",
        ).count()
        self.assertEqual(active_incidents_count, 1)

        # 4. Recover stock: Post 4000 Mtr more -> Total available = 11,000 Mtr (above 10,000)
        self._post_opening(self.item_grey_2, self.godown_sec, "4000.00", taka_no="2")
        incident.refresh_from_db()
        self.assertEqual(incident.status, "Resolved")

    def test_threshold_persistence_and_unset_state(self):
        """Unset threshold reports Unconfigured and does not fabricate alerts."""
        setting = stock.get_combined_grey_stock_setting(self.client_obj.id)
        self.assertFalse(setting["is_configured"])
        self.assertEqual(setting["threshold"], D("0.0000"))

        # Evaluate alert with unconfigured threshold -> No incident created
        res = stock.evaluate_combined_grey_stock_alert(self.client_obj.id)
        self.assertFalse(res["alert_triggered"])
        self.assertIsNone(res["incident"])
        self.assertEqual(AlertIncident.objects.filter(client_id=self.client_obj.id).count(), 0)

        # Set threshold
        updated = stock.set_combined_grey_stock_setting(
            client_id=self.client_obj.id,
            threshold=D("15000.00"),
            is_enabled=True,
            user=self.user,
        )
        self.assertTrue(updated["is_configured"])
        self.assertEqual(updated["threshold"], D("15000.0000"))

        # Individual item reorder levels remain isolated
        self.assertEqual(self.item_grey_1.reorder_level, D("1000.0000"))
        self.assertEqual(self.item_grey_2.reorder_level, D("1500.0000"))

    def test_api_combined_grey_stock_and_setting(self):
        """HTTP endpoints for combined grey stock and settings function correctly."""
        self._post_opening(self.item_grey_1, self.godown_main, "5000.00", taka_no="1")

        # 1. Update setting via API
        resp = self.api.post(
            "/api/v1/inventory/combined-grey-stock/setting/",
            {"threshold": 8000, "isEnabled": True, "uom": "Mtr"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["threshold"], 8000.0)
        self.assertTrue(resp.data["isConfigured"])

        # 2. Get combined grey stock via API
        resp_stock = self.api.get("/api/v1/inventory/combined-grey-stock/")
        self.assertEqual(resp_stock.status_code, 200)
        self.assertEqual(resp_stock.data["physicalGrey"], 5000.0)
        self.assertEqual(resp_stock.data["availableGrey"], 5000.0)
        self.assertEqual(resp_stock.data["threshold"], 8000.0)
        self.assertEqual(resp_stock.data["shortage"], 3000.0)
        self.assertEqual(resp_stock.data["status"], "Low Stock")

        # 3. Get Stock Positions with detail=true (Dashboard view)
        resp_dashboard = self.api.get("/api/v1/inventory/stock/?detail=true")
        self.assertEqual(resp_dashboard.status_code, 200)
        self.assertIn("combinedGreyStock", resp_dashboard.data)
        self.assertEqual(resp_dashboard.data["combinedGreyStock"]["availableGrey"], 5000.0)
        self.assertEqual(resp_dashboard.data["combinedGreyStock"]["shortage"], 3000.0)
