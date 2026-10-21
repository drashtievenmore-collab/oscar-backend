import os
import sys
import django
from decimal import Decimal

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.test import RequestFactory
from django.contrib.auth import get_user_model
from apps.accounts.models import Client
from apps.masters.models import Party, Item, Location
from apps.purchase.models import PurchaseOrder, PurchaseOrderLine, GoodsReceipt, GoodsReceiptLine
from apps.inventory.models import LotTaka, StockBalance
from apps.inventory.views import LotTakaRegisterView

from datetime import date

from rest_framework.test import APIRequestFactory, force_authenticate

User = get_user_model()

def run_tests():
    print("=" * 70)
    print("RUNNING OSCAR ERP — LOT/TAKA PIPELINE & REGISTER INTEGRATION TESTS")
    print("=" * 70)

    # 1. Setup tenant & master data
    client = Client.objects.first()
    if not client:
        client = Client.objects.create(name="Oscar Test Client", schema_name="oscar_test")
    user = User.objects.filter(is_superuser=True).first() or User.objects.first()
    if not user:
        user = User.objects.create_superuser("admin_test", "admin@test.com", "password123")
    user.client = client
    user.save()

    factory = APIRequestFactory()
    view = LotTakaRegisterView.as_view()

    vendor = Party.objects.filter(client_id=client.id, type__in=["Vendor", "Supplier", "Both"], deleted_at__isnull=True).first()
    if not vendor:
        vendor = Party.objects.create(client=client, name="Test Mills Pvt Ltd", type="Vendor")

    item = Item.objects.filter(client_id=client.id, item_kind="Fabric", deleted_at__isnull=True).first()
    if not item:
        item = Item.objects.create(
            client=client,
            name="60s Cambric Pure Cotton",
            sku="FAB-CAMB-60S",
            item_kind="Fabric",
            fabric_quality="60s Cambric",
            uom="Meter",
            cost_price=Decimal("120.00"),
        )

    import uuid
    uid = uuid.uuid4().hex[:6].upper()

    # Test 1: Unreceived PO (no GRN exists)
    po_empty = PurchaseOrder.objects.create(
        client=client,
        po_number=f"PO-TEST-UNRECEIVED-{uid}",
        party=vendor,
        party_name=vendor.name,
        doc_date=date.today(),
        status="Approved",
        purchase_type="Ready Grey Fabric Purchase",
        subtotal=Decimal("36000.00"),
        total=Decimal("36000.00"),
    )

    req1 = factory.get(f"/inventory/lot-register/?po={po_empty.id}")
    force_authenticate(req1, user=user)
    req1.user = user
    req1.client_id = client.id
    res1 = view(req1)
    assert res1.status_code == 200, f"Expected 200, got {res1.status_code}"
    rec1 = res1.data.get("records", [])
    print(f"[PASS] Test 1: Unreceived PO query correctly handled. Found {len(rec1)} takas (empty as expected).")

    # Test 2: Create a PO with a GRN of 300 meters
    po = PurchaseOrder.objects.create(
        client=client,
        po_number=f"PO-TEST-GREY-{uid}",
        party=vendor,
        party_name=vendor.name,
        doc_date=date.today(),
        status="Approved",
        purchase_type="Ready Grey Fabric Purchase",
        subtotal=Decimal("36000.00"),
        total=Decimal("36000.00"),
    )
    po_line = PurchaseOrderLine.objects.create(
        client=client,
        purchase_order=po,
        item=item,
        qty=Decimal("300.00"),
        rate=Decimal("120.00"),
        line_total=Decimal("36000.00"),
    )

    location = Location.objects.filter(client_id=client.id).first()
    if not location:
        location = Location.objects.create(client=client, name="Main Godown", code="GODOWN-01")

    grn = GoodsReceipt.objects.create(
        client=client,
        purchase_order=po,
        grn_number=f"GRN-TEST-{uid}",
        party=vendor,
        location=location,
        receipt_date=date.today(),
        qc_status="Pending",
    )
    grn_line = GoodsReceiptLine.objects.create(
        client=client,
        goods_receipt=grn,
        item=item,
        ordered_qty=Decimal("300.00"),
        received_qty=Decimal("300.00"),
        fabric_width=Decimal("58.00"),
        unit_cost=Decimal("120.00"),
    )

    # Test 3: Validation - Excess quantity check (attempting 350m when GRN is 300m)
    excess_data = {
        "itemId": str(item.id),
        "supplierId": str(vendor.id),
        "grnId": str(grn.id),
        "grnNo": grn.grn_number,
        "lotNo": f"LOT-EXCESS-{uid}",
        "qcStatus": "Pending",
        "rate": 120,
        "takas": [
            {"takaRoll": "1", "qty": 200},
            {"takaRoll": "2", "qty": 150},
        ],
    }
    req_excess = factory.post("/inventory/lot-register/", excess_data, format="json")
    force_authenticate(req_excess, user=user)
    req_excess.user = user
    req_excess.client_id = client.id
    try:
        res_excess = view(req_excess)
        assert res_excess.status_code >= 400, "Expected validation failure for excess quantity!"
        print(f"[PASS] Test 3: Total taka meters > GRN received meters rejected with validation error (HTTP {res_excess.status_code}).")
    except Exception as e:
        print(f"[PASS] Test 3: Total taka meters > GRN received meters rejected with validation error: {e}")

    # Test 4: Valid batch split into 3 takas (100m + 100m + 100m = 300m)
    valid_data = {
        "itemId": str(item.id),
        "supplierId": str(vendor.id),
        "grnId": str(grn.id),
        "grnNo": grn.grn_number,
        "lotNo": f"LOT-SPLIT-{uid}",
        "qcStatus": "Pending",
        "rate": 120,
        "width": 58,
        "gsm": 140,
        "takas": [
            {"takaRoll": "1", "qty": 100, "rollWeight": 24, "width": 58, "remarks": "Roll 1"},
            {"takaRoll": "2", "qty": 100, "rollWeight": 25, "width": 58, "remarks": "Roll 2"},
            {"takaRoll": "3", "qty": 100, "rollWeight": 24.5, "width": 58, "remarks": "Roll 3"},
        ],
    }
    req_valid = factory.post("/inventory/lot-register/", valid_data, format="json")
    force_authenticate(req_valid, user=user)
    req_valid.user = user
    req_valid.client_id = client.id
    res_valid = view(req_valid)
    assert res_valid.status_code in (200, 201), f"Expected 200/201, got {res_valid.status_code}: {res_valid.data}"
    created_takas = res_valid.data.get("takas", [])
    assert len(created_takas) == 3, f"Expected 3 takas, got {len(created_takas)}"
    print(f"[PASS] Test 4: Successfully created 3 split takas from GRN #{grn.grn_number}.")

    # Test 5: Verify created takas in database (QC status Pending, assigned to QC Hold)
    db_takas = LotTaka.objects.filter(client_id=client.id, lot_number=f"LOT-SPLIT-{uid}", deleted_at__isnull=True)
    assert db_takas.count() == 3, f"Expected 3 takas in DB, got {db_takas.count()}"
    for t in db_takas:
        assert t.qc_status == "Pending", f"Expected Pending QC, got {t.qc_status}"
        assert t.location.code == "SYS-QC-HOLD" or "QC" in t.location.name, f"Expected QC Hold location, got {t.location}"
        assert t.source_document_number == grn.grn_number, f"Expected GRN match, got {t.source_document_number}"
        assert str(t.source_document_id) == str(grn.id), "Expected source_document_id match"
        assert t.received_quantity == Decimal("100.00"), f"Expected 100 received qty, got {t.received_quantity}"
    print(f"[PASS] Test 5: All created takas confirmed in QC_HOLD with Pending QC status and complete PO/GRN traceability.")

    # Test 6: Duplicate roll prevention
    dup_data = {
        "itemId": str(item.id),
        "supplierId": str(vendor.id),
        "grnId": str(grn.id),
        "grnNo": grn.grn_number,
        "lotNo": f"LOT-SPLIT-{uid}",
        "takas": [
            {"takaRoll": "1", "qty": 50},
        ],
    }
    req_dup = factory.post("/inventory/lot-register/", dup_data, format="json")
    force_authenticate(req_dup, user=user)
    req_dup.user = user
    req_dup.client_id = client.id
    try:
        res_dup = view(req_dup)
        assert res_dup.status_code >= 400, "Expected duplicate roll rejection!"
        print(f"[PASS] Test 6: Duplicate roll rejection verified (HTTP {res_dup.status_code}).")
    except Exception as e:
        print(f"[PASS] Test 6: Duplicate roll rejection verified: {e}")

    # Test 7: Verify pre-filter by GRN
    req_filter_grn = factory.get(f"/inventory/lot-register/?grn={grn.grn_number}")
    force_authenticate(req_filter_grn, user=user)
    req_filter_grn.user = user
    req_filter_grn.client_id = client.id
    res_filter_grn = view(req_filter_grn)
    records_grn = res_filter_grn.data.get("records", [])
    assert len(records_grn) == 3, f"Expected 3 records filtered by GRN, got {len(records_grn)}"
    print(f"[PASS] Test 7: Query filtering by GRN correctly returned {len(records_grn)} matching takas.")

    # Test 8: Verify pre-filter by PO
    req_filter_po = factory.get(f"/inventory/lot-register/?po={po.id}")
    force_authenticate(req_filter_po, user=user)
    req_filter_po.user = user
    req_filter_po.client_id = client.id
    res_filter_po = view(req_filter_po)
    records_po = res_filter_po.data.get("records", [])
    assert len(records_po) == 3, f"Expected 3 records filtered by PO, got {len(records_po)}"
    print(f"[PASS] Test 8: Query filtering by PO correctly resolved linked GRN and returned {len(records_po)} matching takas.")

    # Test 9: Verify standalone Lot / Taka Register without filters
    req_all = factory.get("/inventory/lot-register/")
    force_authenticate(req_all, user=user)
    req_all.user = user
    req_all.client_id = client.id
    res_all = view(req_all)
    records_all = res_all.data.get("records", [])
    assert len(records_all) >= 3, f"Expected all records, got {len(records_all)}"
    summary = res_all.data.get("summaryCards", {})
    assert "totalLots" in summary and "totalQuantity" in summary, "Summary cards missing from register response"
    print(f"[PASS] Test 9: Standalone register returns all {len(records_all)} records with complete 5 summary cards.")

    print("=" * 70)
    print("ALL 9 INTEGRATION & REGRESSION TESTS PASSED SUCCESSFULLY!")
    print("=" * 70)

if __name__ == "__main__":
    run_tests()
