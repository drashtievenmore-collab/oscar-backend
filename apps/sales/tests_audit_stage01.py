"""Audit Stage 0 / Stage 1 regression tests (Oct 2026 audit).

* a cancelled invoice nets to zero in the ledger and the party balance
* a credit note is valued at the invoice's own terms, not client qty/rate
* posted returns and payments can't be edited or deleted
* payment allocations: sum <= payment, same party, not on a cancelled receipt
* approval decisions are tenant-scoped and need an approver
* PO status can't be set by a plain create / edit
* Reset Demo Data refuses a real tenant even with DEBUG on
* masters created without a code no longer crash
"""
from decimal import Decimal

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting import services as ledger
from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.exceptions import BusinessRuleViolation, ValidationFailed
from apps.core.models import ApprovalRequest
from apps.masters.models import Party
from apps.purchase.models import PurchaseBill
from apps.purchase import services as purchase_services

from . import services as sales_services
from .models import PaymentIn, SalesInvoice
from .tests_phase1 import Phase1Base


class ReversalNettingTests(Phase1Base):
    def _finalized_invoice(self, qty=10, rate=100):
        created = self.post("/sales/invoices/", {
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "locationId": str(self.location.id),
            "lineItems": [{"itemId": str(self.fabric.id), "qty": qty, "rate": rate, "tax": 0}],
        })
        self.assertEqual(created.status_code, 201, created.content[:600])
        invoice_id = created.json()["id"]
        self.assertEqual(self.post(f"/sales/invoices/{invoice_id}/finalize/").status_code, 200)
        return invoice_id

    def test_cancelled_invoice_nets_to_zero_everywhere(self):
        invoice_id = self._finalized_invoice()
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, Decimal("1000.00"))

        cancelled = self.post(f"/sales/invoices/{invoice_id}/cancel/", {"reason": "test"})
        self.assertEqual(cancelled.status_code, 200, cancelled.content[:600])

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, Decimal("0.00"))
        rows = ledger.trial_balance(self.tenant.id)
        rows = rows.get("rows", rows) if isinstance(rows, dict) else rows
        by_key = {}
        for row in rows:
            key = row.get("systemKey") or row.get("system_key") or row.get("code")
            by_key[key] = Decimal(str(row.get("debit", 0))) - Decimal(str(row.get("credit", 0)))
        for key in ("debtors", "sales", "1100", "4000"):
            if key in by_key:
                self.assertEqual(by_key[key], Decimal("0"), f"{key} should net to zero")

    def test_document_reversal_never_reverses_the_reversal(self):
        invoice_id = self._finalized_invoice()
        self.post(f"/sales/invoices/{invoice_id}/cancel/", {"reason": "test"})
        again = ledger.reverse_document_entries(
            client_id=self.tenant.id, source_document_type="SalesInvoice",
            source_document_id=invoice_id,
        )
        self.assertEqual([e for e in again if e is not None], [])


class CreditNoteValuationTests(Phase1Base):
    def test_credit_note_uses_invoice_rate_and_returned_qty(self):
        order = self.order(qty=10, rate=100)
        challan_id = self.dispatch(order["id"])
        invoice = self.post(f"/sales/challans/{challan_id}/convert-to-invoice/").json()
        self.assertEqual(self.post(f"/sales/invoices/{invoice['id']}/finalize/").status_code, 200)
        line = SalesInvoice.objects.get(pk=invoice["id"]).line_items.get()

        created = self.post("/sales/returns/", {
            "salesInvoiceId": invoice["id"],
            "partyId": str(self.customer.id),
            "date": str(timezone.localdate()),
            "locationId": str(self.location.id),
            # 1 m physically returned, but an inflated qty and rate are sent.
            "lineItems": [{
                "itemId": str(self.fabric.id), "qty": 100, "returnedQty": 1, "rate": 9999,
                "tax": 0, "salesInvoiceLineId": str(line.id),
            }],
        })
        self.assertEqual(created.status_code, 201, created.content[:800])
        body = created.json()
        self.assertEqual(Decimal(str(body["total"])), Decimal("100.00"))
        return_id = body["id"]

        # Posted: lines can't be rewritten and it can't be deleted...
        edit = self.api.patch(f"/api/v1/sales/returns/{return_id}/", {
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 1, "returnedQty": 1, "rate": 1,
                           "salesInvoiceLineId": str(line.id)}],
        }, format="json")
        self.assertEqual(edit.status_code, 409, edit.content[:400])
        self.assertEqual(self.api.delete(f"/api/v1/sales/returns/{return_id}/").status_code, 409)
        # ...but a note can still be added.
        note = self.api.patch(f"/api/v1/sales/returns/{return_id}/", {"notes": "checked"}, format="json")
        self.assertEqual(note.status_code, 200, note.content[:400])


class PaymentGuardTests(Phase1Base):
    def _bill(self, number, total, party=None):
        return PurchaseBill.objects.create(
            client=self.tenant, party=party or self.vendor, party_name=(party or self.vendor).name,
            doc_date=timezone.localdate(), bill_number=number, status="Unpaid",
            posted_at=timezone.now(), subtotal=total, taxable_value=total, total=total,
        )

    def test_payment_out_allocations_cannot_exceed_payment(self):
        a, b = self._bill("PB-A", Decimal("1000")), self._bill("PB-B", Decimal("1000"))
        with self.assertRaises(BusinessRuleViolation):
            purchase_services.record_payment_out(
                client=self.tenant, party=self.vendor, amount=Decimal("1000"),
                payment_date=timezone.localdate(), mode="Cash",
                allocations=[{"billId": a.id, "amount": 1000}, {"billId": b.id, "amount": 1000}],
            )

    def test_payment_out_rejects_another_vendors_bill(self):
        other = Party.objects.create(client=self.tenant, code="V-OTH", name="Other", type="Vendor")
        bill = self._bill("PB-O", Decimal("500"), party=other)
        with self.assertRaises(ValidationFailed):
            purchase_services.record_payment_out(
                client=self.tenant, party=self.vendor, amount=Decimal("500"),
                payment_date=timezone.localdate(), mode="Cash",
                allocations=[{"billId": bill.id, "amount": 500}],
            )

    def test_cancelled_receipt_cannot_be_allocated(self):
        payment = PaymentIn.objects.create(
            client=self.tenant, payment_number="RCPT-T1", party=self.customer,
            payment_date=timezone.localdate(), amount=Decimal("100"), mode="Cash",
            status="Cancelled",
        )
        with self.assertRaises(BusinessRuleViolation):
            sales_services.allocate_payment_in(payment, [])

    def test_payments_are_not_editable_or_deletable(self):
        payment = PaymentIn.objects.create(
            client=self.tenant, payment_number="RCPT-T2", party=self.customer,
            payment_date=timezone.localdate(), amount=Decimal("100"), mode="Cash",
        )
        self.assertEqual(
            self.api.patch(f"/api/v1/sales/payments/{payment.id}/", {"amount": 1}, format="json").status_code, 405
        )
        self.assertEqual(self.api.delete(f"/api/v1/sales/payments/{payment.id}/").status_code, 405)


class ApprovalScopeTests(Phase1Base):
    def setUp(self):
        super().setUp()
        self.other_tenant = Client.objects.create(slug="audit-other", name="Other Co")
        self.outsider = User.objects.create_user(
            email="outsider@example.com", password="pass", client=self.other_tenant,
        )
        self.clerk = User.objects.create_user(
            email="clerk@example.com", password="pass", client=self.tenant,
        )
        self.request = ApprovalRequest.objects.create(
            client=self.tenant, document_type="discount", document_id="doc-1", status="Pending",
            requested_by=self.user,
        )

    def _as(self, user):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return api

    def test_other_tenant_cannot_decide(self):
        response = self._as(self.outsider).post(f"/api/v1/approvals/requests/{self.request.id}/approve/")
        self.assertEqual(response.status_code, 404, response.content[:300])
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, "Pending")

    def test_non_approver_cannot_decide(self):
        response = self._as(self.clerk).post(f"/api/v1/approvals/requests/{self.request.id}/approve/")
        self.assertEqual(response.status_code, 403, response.content[:300])

    def test_decision_fields_are_not_writable(self):
        created = self._as(self.clerk).post("/api/v1/approvals/requests/", {
            "document_type": "discount", "document_id": "doc-2", "status": "Approved",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content[:300])
        self.assertEqual(created.json()["status"], "Pending")
        self.assertEqual(
            self.api.patch(f"/api/v1/approvals/requests/{self.request.id}/", {"status": "Approved"},
                           format="json").status_code, 405,
        )


class PurchaseOrderStatusTests(Phase1Base):
    def _create(self, **extra):
        return self.post("/purchase/orders/", {
            "vendorId": str(self.vendor.id), "date": str(timezone.localdate()),
            "lineItems": [{"itemId": str(self.fabric.id), "qty": 10, "rate": 50, "tax": 0}],
            **extra,
        })

    def test_po_cannot_be_created_approved(self):
        self.assertEqual(self._create(status="Approved").status_code, 400)

    def test_po_status_cannot_be_patched(self):
        created = self._create()
        self.assertEqual(created.status_code, 201, created.content[:600])
        po_id = created.json()["id"]
        patched = self.api.patch(f"/api/v1/purchase/orders/{po_id}/", {"status": "Approved"}, format="json")
        self.assertEqual(patched.status_code, 400, patched.content[:400])


class SafetySwitchTests(Phase1Base):
    @override_settings(DEBUG=True)
    def test_reset_demo_refuses_real_tenant_even_in_debug(self):
        self.tenant.is_demo = False
        self.tenant.save(update_fields=["is_demo"])
        response = self.post("/settings/reset-demo-data/")
        self.assertEqual(response.status_code, 403, response.content[:300])

    def test_master_without_code_is_created(self):
        response = self.post("/masters/processes/", {"name": "Dyeing Audit"})
        self.assertEqual(response.status_code, 201, response.content[:400])
        self.assertTrue(response.json().get("code"))

    def test_new_system_accounts_resolve(self):
        for key in ("brokerage_payable", "brokerage_expense", "sample_expense"):
            self.assertIsNotNone(ledger.system_account(self.tenant.id, key))
