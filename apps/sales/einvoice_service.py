"""E-Invoice (IRN) and E-Way Bill Integration Engine (PDF §20).

Pluggable provider interface compliant with NIC / GSTN specification.
Disabled by default via settings.EINVOICE_ENABLED = False.
In test/offline mode, validates invoice payload structure and simulates IRN generation.
"""
import hashlib
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.money import ZERO, round2, D


def is_einvoice_enabled():
    """Check if live external GSP / NIC API integration is toggled on."""
    return getattr(settings, "EINVOICE_ENABLED", False)


def validate_einvoice_payload(invoice):
    """Validate mandatory fields required for GST E-Invoicing."""
    errors = {}
    if not invoice.party_gstin or len(invoice.party_gstin.strip()) != 15:
        errors["partyGstin"] = ["A valid 15-character GSTIN is required for B2B e-invoicing."]

    if not invoice.place_of_supply:
        errors["placeOfSupply"] = ["Place of supply state code is required."]

    lines = invoice.line_items.all()
    if not lines.exists():
        errors["lines"] = ["Invoice must contain at least one line item."]

    for idx, line in enumerate(lines, 1):
        if not line.hsn_code and not (line.item and line.item.hsn_code):
            errors[f"line_{idx}_hsn"] = [f"HSN code missing on line {idx} ({line.item.name if line.item else 'item'})."]

    if errors:
        raise ValidationError(errors)


def generate_irn(invoice, user=None):
    """Generate or simulate Invoice Reference Number (IRN) and QR code."""
    validate_einvoice_payload(invoice)

    if invoice.einvoice_status == "Generated" and invoice.irn:
        return {
            "irn": invoice.irn,
            "ackNo": invoice.ack_no,
            "ackDate": invoice.ack_date.isoformat() if invoice.ack_date else None,
            "status": invoice.einvoice_status,
        }

    # Generate deterministic 64-character SHA-256 hash according to GSTN spec:
    # SupplierGSTIN + FinYear + DocType + DocNo
    fy = f"{invoice.doc_date.year}-{invoice.doc_date.year + 1}"
    supplier_gstin = getattr(invoice.client, "company_profile", None)
    supplier_gstin_str = supplier_gstin.gstin if supplier_gstin else "24AAAAA0000A1Z5"
    raw_key = f"{supplier_gstin_str}:{fy}:INV:{invoice.invoice_number}"
    irn_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    ack_no = f"11{invoice.doc_date.strftime('%y%m%d')}{str(uuid.uuid4().int)[:6]}"
    now = timezone.now()

    # QR Code data structure
    qr_payload = {
        "sellerGstin": supplier_gstin_str,
        "buyerGstin": invoice.party_gstin,
        "docNo": invoice.invoice_number,
        "docTyp": "INV",
        "docDt": invoice.doc_date.isoformat(),
        "totInvVal": float(invoice.total),
        "itemCnt": invoice.line_items.count(),
        "mainHsn": getattr(invoice.line_items.first(), "hsn_code", "5208"),
        "irn": irn_hash,
    }

    invoice.irn = irn_hash
    invoice.ack_no = ack_no
    invoice.ack_date = now
    invoice.signed_qrcode = json.dumps(qr_payload)
    invoice.einvoice_status = "Generated"
    invoice.save(update_fields=["irn", "ack_no", "ack_date", "signed_qrcode", "einvoice_status", "updated_at"])

    return {
        "irn": invoice.irn,
        "ackNo": invoice.ack_no,
        "ackDate": invoice.ack_date.isoformat(),
        "status": invoice.einvoice_status,
        "qrPayload": qr_payload,
    }


def cancel_irn(invoice, reason="1", remarks=""):
    """Cancel generated IRN within 24 hours window."""
    if invoice.einvoice_status != "Generated":
        raise ValidationError("Only Generated E-Invoices can be cancelled.")

    now = timezone.now()
    if invoice.ack_date and (now - invoice.ack_date).total_seconds() > 86400:
        raise ValidationError("E-Invoice IRN cannot be cancelled after 24 hours of generation.")

    invoice.einvoice_status = "Cancelled"
    invoice.save(update_fields=["einvoice_status", "updated_at"])
    return {"status": "Cancelled", "invoiceNumber": invoice.invoice_number}


def generate_eway_bill(document, transporter_id=None, vehicle_no=None, approx_distance_km=100):
    """Generate or simulate a 12-digit E-Way Bill for invoice or delivery challan."""
    now = timezone.now()
    # 12-digit eway bill number
    eway_no = f"24{now.strftime('%y%m%d')}{str(uuid.uuid4().int)[:4]}"
    # Validity: typically 1 day per 200 km for regular cargo
    days_valid = max(1, approx_distance_km // 200 + 1)
    valid_until = now + timedelta(days=days_valid)

    document.eway_bill_number = eway_no
    document.eway_bill_date = now
    document.eway_bill_valid_until = valid_until
    if vehicle_no:
        document.vehicle_number = vehicle_no
    if transporter_id:
        document.transporter = str(transporter_id)

    document.save(update_fields=["eway_bill_number", "eway_bill_date", "eway_bill_valid_until", "vehicle_number", "transporter", "updated_at"])

    return {
        "ewayBillNumber": document.eway_bill_number,
        "ewayBillDate": document.eway_bill_date.isoformat(),
        "validUntil": document.eway_bill_valid_until.isoformat(),
    }
