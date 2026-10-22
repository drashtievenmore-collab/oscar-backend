"""P6: job-work costing, processor reconciliation, sampling and lot merge.

PDF §06 lot merge, §07 reconciliation (100 sent, 50 + 30 back, 20 pending),
§08 reprocess cost on the same lot, §09 job-work charges in the books, §10
sampling as a stock movement.
"""
import uuid
from datetime import date
from decimal import Decimal

from django.db.models import Sum
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounting import services as ledger
from apps.accounting.models import JournalLine
from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.inventory import costing_services
from apps.inventory.models import LotCostEntry, LotTaka, StockMovement
from apps.masters.models import Item, Location, Party


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class P6Base(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("p6"), name="P6 Tenant")
        self.user = User.objects.create_user(
            email=f"{uid('p6')}@example.com", password="pass", client=self.tenant, is_superuser=True
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")
        ledger.seed_chart_of_accounts(self.tenant)
        self.item = Item.objects.create(
            client=self.tenant, sku=uid("RAY"), name="Rayon 30s", tracking_type="TAKA", uom="Meter",
        )
        self.godown = Location.objects.create(
            client=self.tenant, code=uid("WH"), name="Main Godown", type="Godown", is_active=True
        )
        self.processor = Party.objects.create(
            client=self.tenant, name="Apex Processing House", code=uid("PROC"), type="Processor"
        )

    def taka(self, lot_number, qty, rate="50", status="Available", **extra):
        taka = LotTaka.objects.create(
            client=self.tenant, lot_number=lot_number, taka_number="1", taka_id=uid("TAK"),
            item=self.item, location=self.godown, received_quantity=Decimal("0"),
            available_quantity=Decimal("0"), status=status, received_date=date(2026, 4, 1),
            uom="Meter", rate=Decimal(rate), **extra,
        )
        # Opening stock through the ledger, so balance and taka agree.
        from apps.inventory import services as stock

        stock.post_movement(
            client_id=self.tenant.id, item=self.item, location=self.godown, type="ADJUSTMENT",
            quantity=Decimal(qty), unit_cost=Decimal(rate), lot_taka=taka, notes="opening",
        )
        LotTaka.objects.filter(pk=taka.pk).update(received_quantity=Decimal(qty))
        taka.refresh_from_db()
        return taka

    def order(self, rate=10):
        resp = self.api.post("/api/v1/jobwork/orders/", {
            "vendorId": str(self.processor.id), "vendor": self.processor.name, "process": "Dyeing",
            "orderDate": "2026-04-01", "rate": rate, "plannedQty": 100,
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:400])
        return resp.json()["id"]

    def row(self, order_id, kind, data, method="post", row_id=None):
        url = f"/api/v1/jobwork/orders/{order_id}/{kind}/" + (f"{row_id}/" if row_id else "")
        resp = getattr(self.api, method)(url, data, format="json")
        self.assertIn(resp.status_code, (200, 201, 204), resp.content[:400])
        return resp.json() if resp.content else None

    def accrued(self, source_type):
        totals = JournalLine.objects.filter(
            client_id=self.tenant.id, account__system_key="jobwork_payable",
            journal_entry__source_document_type=source_type,
        ).aggregate(d=Sum("debit"), c=Sum("credit"))
        return (totals["c"] or Decimal("0")) - (totals["d"] or Decimal("0"))


class JobWorkCostingTests(P6Base):
    def send_and_receive(self, sent=100, received=90):
        grey = self.taka("LOT-GREY-1", sent)
        order_id = self.order(rate=10)
        self.row(order_id, "outwards", {
            "date": "2026-04-02", "qty": sent,
            "takas": [{"lotTakaId": str(grey.id), "meters": sent}],
        })
        inward = self.row(order_id, "inwards", {
            "date": "2026-04-05", "qty": received, "accepted": received, "rejected": 0,
            "takas": [{"sourceTakaId": str(grey.id), "taka_number": "1", "meters": received}],
        })
        return order_id, inward, f"{grey.lot_number}-P"

    def test_processing_charges_reach_lot_and_books(self):
        _, _, finished_lot = self.send_and_receive()
        cost = costing_services.get_lot_landed_cost(self.tenant.id, finished_lot)
        self.assertEqual(Decimal(cost["breakdown"]["processing"]), Decimal("900.00"))
        self.assertEqual(self.accrued("JobWorkInward"), Decimal("900.00"))
        line = JournalLine.objects.get(
            client_id=self.tenant.id, account__system_key="jobwork_payable",
            journal_entry__source_document_type="JobWorkInward",
        )
        self.assertEqual(line.party_id, self.processor.id)

    def test_reprocess_cost_lands_on_the_reprocessed_lot(self):
        order_id, inward, finished_lot = self.send_and_receive()
        rp = self.row(order_id, "reprocesses", {
            "inwardNo": inward["no"], "date": "2026-04-06", "qty": 40, "rate": 5, "reason": "Shade",
        })
        cost = costing_services.get_lot_landed_cost(self.tenant.id, finished_lot)
        self.assertEqual(Decimal(cost["breakdown"]["reprocess"]), Decimal("200.00"))
        self.assertEqual(Decimal(cost["totalCost"]), Decimal("1100.00"))  # 900 processing + 200 reprocess
        self.assertFalse(LotCostEntry.objects.filter(client_id=self.tenant.id, lot_number__startswith="JWO-").exists())
        self.assertEqual(self.accrued("JWOReprocess"), Decimal("200.00"))

        # Editing the round posts only the difference; nothing is overwritten.
        self.row(order_id, "reprocesses", {"qty": 60}, method="patch", row_id=rp["id"])
        entries = LotCostEntry.objects.filter(client_id=self.tenant.id, cost_type="Reprocess")
        self.assertEqual(entries.count(), 2)
        self.assertEqual(sum(e.amount for e in entries), Decimal("300.00"))  # 60 x 5 kept rate
        self.assertEqual(self.accrued("JWOReprocess"), Decimal("300.00"))

        # Deleting it reverses both ledgers.
        self.row(order_id, "reprocesses", None, method="delete", row_id=rp["id"])
        self.assertEqual(sum(e.amount for e in LotCostEntry.objects.filter(
            client_id=self.tenant.id, cost_type="Reprocess")), Decimal("0"))
        self.assertEqual(self.accrued("JWOReprocess"), Decimal("0"))

    def test_charge_rows_are_accrued_and_reversed(self):
        order_id = self.order()
        charge = self.row(order_id, "charges", {"type": "Transport", "rate": 1200, "qty": 1, "amount": 1200})
        self.assertEqual(self.accrued("JWOCharge"), Decimal("1200.00"))
        self.row(order_id, "charges", None, method="delete", row_id=charge["id"])
        self.assertEqual(self.accrued("JWOCharge"), Decimal("0"))


class ProcessorReconciliationTests(P6Base):
    def setUp(self):
        super().setUp()
        self.order_id = self.order()
        # PDF §07: 100 M sent -> 50 M received -> 30 M received -> 20 M pending.
        # The receipts are entered out of date order on purpose.
        self.row(self.order_id, "outwards", {"date": "2026-04-01", "qty": 100})
        self.row(self.order_id, "inwards", {"date": "2026-04-05", "qty": 30, "accepted": 30, "rejected": 0})
        self.row(self.order_id, "inwards", {"date": "2026-04-03", "qty": 50, "accepted": 50, "rejected": 0})

    def ledger_for(self, **params):
        resp = self.api.get("/api/v1/jobwork/orders/processor-ledger/", params)
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        return resp.json()["results"]

    def test_pdf_example_and_running_balance_in_date_order(self):
        rows = self.ledger_for(processorId=str(self.processor.id))
        self.assertEqual(rows[0]["closingBalance"], 20.0)
        balances = [(e["date"], e["balanceQty"]) for e in rows[0]["entries"]]
        self.assertEqual(balances, [("2026-04-01", 100.0), ("2026-04-03", 50.0), ("2026-04-05", 20.0)])

    def test_opening_balance_before_from_date(self):
        rows = self.ledger_for(processorId=str(self.processor.id), fromDate="2026-04-04")
        self.assertEqual(rows[0]["openingBalance"], 50.0)
        self.assertEqual(rows[0]["totalReceived"], 30.0)
        self.assertEqual(rows[0]["closingBalance"], 20.0)

    def test_processor_type_parties_are_listed(self):
        rows = self.ledger_for()
        self.assertIn(str(self.processor.id), [r["processorId"] for r in rows])

    def test_vendor_reconciliation_counts_approved_loss(self):
        from apps.jobwork.models import JobWorkOrder, JWOLossEntry
        from apps.masters.models import LossReasonMaster

        reason = LossReasonMaster.objects.create(client=self.tenant, code=uid("LOSS"), name="Shrinkage")
        JWOLossEntry.objects.create(
            client=self.tenant, job_work_order=JobWorkOrder.objects.get(pk=self.order_id),
            loss_reason=reason, qty=Decimal("20"), status="Approved",
        )
        resp = self.api.get("/api/v1/jobwork/orders/vendor-reconciliation/")
        row = next(r for r in resp.json()["results"] if r["jwoId"] == self.order_id)
        self.assertEqual(row["approvedLossQty"], 20.0)
        self.assertEqual(row["pendingQty"], 0.0)
        self.assertEqual(row["status"], "Reconciled")


class SamplingStockTests(P6Base):
    def test_sample_needs_a_source_taka(self):
        resp = self.api.post("/api/v1/inventory/samples/", {"quantity": 2}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content[:300])

    def test_cut_piece_is_deducted_and_returned_once(self):
        taka = self.taka("LOT-FIN-1", 100, status="Finished")
        resp = self.api.post("/api/v1/inventory/samples/", {
            "sourceLotTakaId": str(taka.id), "quantity": 2, "sampleType": "CUT_PIECE",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        taka.refresh_from_db()
        self.assertEqual(taka.available_quantity, Decimal("98"))

        resp = self.api.post(
            f"/api/v1/inventory/samples/{resp.json()['id']}/return-sample/", {"returnQuantity": 2}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        taka.refresh_from_db()
        self.assertEqual(taka.available_quantity, Decimal("100"))


class LotMergeTests(P6Base):
    def test_merge_three_lots_and_trace_back(self):
        a = self.taka("LOT-A", 100, rate="40")
        b = self.taka("LOT-B", 50, rate="60")
        c = self.taka("LOT-C", 50, rate="50")
        resp = self.api.post("/api/v1/inventory/lot-register/merge/", {
            "lotTakaIds": [str(a.id), str(b.id), str(c.id)], "lotNumber": "BATCH-1",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content[:400])
        merged = LotTaka.objects.get(pk=resp.json()["id"])
        self.assertEqual(merged.available_quantity, Decimal("200"))
        self.assertEqual(merged.rate, Decimal("47.5"))  # (4000 + 3000 + 2500) / 200
        for src in (a, b, c):
            src.refresh_from_db()
            self.assertEqual(src.available_quantity, Decimal("0"))
        self.assertEqual(
            StockMovement.objects.filter(client_id=self.tenant.id, type="LOT_MERGE_OUT").count(), 3
        )
        self.assertEqual(
            Decimal(costing_services.get_lot_landed_cost(self.tenant.id, "BATCH-1")["totalCost"]),
            Decimal("9500.00"),
        )

        # A finished lot processed from the batch traces back to all three.
        finished = LotTaka.objects.create(
            client=self.tenant, lot_number="BATCH-1-P", taka_number="1", taka_id=uid("TAK"),
            item=self.item, location=self.godown, received_quantity=Decimal("190"),
            available_quantity=Decimal("0"), status="Finished", parent_taka=merged, uom="Meter",
        )
        trace = self.api.get(f"/api/v1/inventory/traceability/?target={finished.id}").json()
        lots = {anc["lotNo"] for anc in trace["ancestors"]}
        self.assertEqual(lots, {"BATCH-1", "LOT-A", "LOT-B", "LOT-C"})

    def test_merge_refuses_mixed_godowns(self):
        a = self.taka("LOT-A", 10)
        other = Location.objects.create(client=self.tenant, code=uid("WH"), name="Other", type="Godown", is_active=True)
        b = self.taka("LOT-B", 10)
        LotTaka.objects.filter(pk=b.pk).update(location=other)
        resp = self.api.post("/api/v1/inventory/lot-register/merge/", {
            "lotTakaIds": [str(a.id), str(b.id)],
        }, format="json")
        self.assertEqual(resp.status_code, 422, resp.content[:300])
