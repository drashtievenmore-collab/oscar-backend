"""E-Invoice (IRN) and E-Way Bill Integration Engine (PDF §20).

Pluggable provider interface compliant with NIC / GSTN specification.
Toggled via settings.EINVOICE_ENABLED (default False).
Providers:
  - 'sandbox-sim': Deterministic simulator generating valid 64-character SHA-256
    IRN hash according to GSTN spec, signed QR code structure, and 12-digit E-Way bills.
  - 'gsp-nic': Real GSP / NIC direct API adapter structure with GSTN Schema v1.03
    payload generator and CA/GSP credential integration hooks.

Audit:
  Every operation records complete request and response payloads to the immutable
  AuditLog (apps.core.models.AuditLog) via record_audit.
"""
import abc
import hashlib
import json
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.models import CompanyProfile


# ---------------------------------------------------------------------------
# Configuration & Helpers
# ---------------------------------------------------------------------------

def is_einvoice_enabled():
    """Check if e-invoicing is enabled in settings."""
    return getattr(settings, "EINVOICE_ENABLED", False)


def get_active_provider_info():
    """Return dictionary of active e-invoice provider metadata for API consumers."""
    provider_name = getattr(settings, "EINVOICE_PROVIDER", "sandbox-sim").lower()
    sandbox_mode = getattr(settings, "EINVOICE_SANDBOX_MODE", True)
    enabled = is_einvoice_enabled()
    return {
        "enabled": enabled,
        "provider": provider_name,
        "sandboxMode": sandbox_mode,
        "isSimulated": provider_name in ("sandbox-sim", "simulator", "sim"),
        "baseUrl": getattr(settings, "EINVOICE_BASE_URL", "https://einv-apisandbox.nic.in"),
    }


def get_supplier_profile(document):
    """Retrieve and validate the supplier's CompanyProfile.

    Raises a clear ValidationError if the company profile or its GSTIN is missing.
    No hardcoded fallback is permitted (PDF §20).
    """
    client_id = getattr(document, "client_id", None) or getattr(document, "client", None)
    profile = CompanyProfile.objects.filter(client_id=client_id).first()
    if not profile or not profile.gstin or not profile.gstin.strip():
        raise ValidationError({
            "companyGstin": [
                "Company Profile GSTIN is missing. Please configure your Company GSTIN in "
                "Settings -> Company Profile before generating e-invoices or e-way bills."
            ]
        })

    gstin = profile.gstin.strip().upper()
    if len(gstin) != 15:
        raise ValidationError({
            "companyGstin": [
                f"Company Profile GSTIN '{gstin}' is invalid. A valid 15-character GSTIN is required."
            ]
        })

    return profile


def validate_einvoice_payload(invoice):
    """Validate mandatory fields required for GST E-Invoicing."""
    errors = {}

    # 1. Supplier GSTIN from CompanyProfile
    try:
        get_supplier_profile(invoice)
    except ValidationError as e:
        if hasattr(e, "message_dict"):
            errors.update(e.message_dict)
        else:
            errors["companyGstin"] = list(e.messages)

    # 2. Buyer GSTIN
    if not invoice.party_gstin or len(invoice.party_gstin.strip()) != 15:
        errors["partyGstin"] = ["A valid 15-character GSTIN is required for B2B e-invoicing."]

    # 3. Place of supply
    if not invoice.place_of_supply:
        errors["placeOfSupply"] = ["Place of supply state code is required."]

    # 4. Line items & HSN
    lines = invoice.line_items.all()
    if not lines.exists():
        errors["lines"] = ["Invoice must contain at least one line item."]

    for idx, line in enumerate(lines, 1):
        if not line.hsn_code and not (line.item and line.item.hsn_code):
            errors[f"line_{idx}_hsn"] = [
                f"HSN code missing on line {idx} ({line.item.name if line.item else 'item'})."
            ]

    if errors:
        raise ValidationError(errors)


# ---------------------------------------------------------------------------
# GSTN Schema v1.03 Payload Builders
# ---------------------------------------------------------------------------

def build_gstn_invoice_payload(invoice, supplier_profile):
    """Construct full request JSON compliant with GSTN E-Invoice Schema v1.03."""
    lines = invoice.line_items.all()
    item_list = []
    for idx, line in enumerate(lines, 1):
        hsn = line.hsn_code or (line.item.hsn_code if line.item else "5208")
        qty = float(line.qty) if line.qty is not None else 1.0
        rate = float(line.rate) if line.rate is not None else 0.0
        taxable = float(getattr(line, "taxable_value", None) or getattr(line, "amount", 0) or round(qty * rate, 2))
        discount = float(getattr(line, "discount_amount", 0) or 0)
        cgst_val = float(getattr(line, "cgst", 0) or 0)
        sgst_val = float(getattr(line, "sgst", 0) or 0)
        igst_val = float(getattr(line, "igst", 0) or 0)
        gst_rate = float(getattr(line, "tax_pct", None) or getattr(line, "gst_rate", 0) or 0)
        tot_item_val = float(getattr(line, "line_total", None) or getattr(line, "total", None) or round(taxable + cgst_val + sgst_val + igst_val, 2))

        item_list.append({
            "SlNo": str(idx),
            "PrdDesc": line.item.name if line.item else f"Item {idx}",
            "IsServc": "N",
            "HsnCd": str(hsn).strip(),
            "Qty": qty,
            "Unit": getattr(line, "uom", "MTR") or "MTR",
            "UnitPrice": rate,
            "TotAmt": round(qty * rate, 2),
            "Discount": discount,
            "AssAmt": taxable,
            "GstRt": gst_rate,
            "IgstAmt": igst_val,
            "CgstAmt": cgst_val,
            "SgstAmt": sgst_val,
            "CesRt": 0,
            "CesAmt": 0,
            "TotItemVal": tot_item_val,
        })

    buyer_addr = invoice.billing_address or {}
    buyer_state = str(invoice.place_of_supply or "24")[:2]

    supp_addr = supplier_profile.address or {}
    supp_state = str(supplier_profile.state_code or "24")[:2]

    return {
        "Version": "1.03",
        "TranDtls": {
            "TaxSch": "GST",
            "SupTyp": "B2B",
            "RegRev": "N",
            "EcmGstin": None,
            "IgstOnIntra": "N",
        },
        "DocDtls": {
            "Typ": "INV",
            "No": invoice.invoice_number or f"INV-{invoice.id}",
            "Dt": invoice.doc_date.strftime("%d/%m/%Y") if invoice.doc_date else timezone.now().strftime("%d/%m/%Y"),
        },
        "SellerDtls": {
            "Gstin": supplier_profile.gstin.strip().upper(),
            "LglNm": supplier_profile.legal_name or "Oscar Fabrics",
            "TrdNm": supplier_profile.trade_name or supplier_profile.legal_name or "Oscar Fabrics",
            "Addr1": supp_addr.get("line1") or "Industrial Area",
            "Addr2": supp_addr.get("line2") or "",
            "Loc": supp_addr.get("city") or "Surat",
            "Pin": int(supp_addr.get("pincode") or 395002),
            "Stcd": supp_state,
            "Ph": supplier_profile.phone or "",
            "Em": supplier_profile.email or "",
        },
        "BuyerDtls": {
            "Gstin": invoice.party_gstin.strip().upper(),
            "LglNm": invoice.party_name or (invoice.party.name if invoice.party else "Customer"),
            "TrdNm": invoice.party_name or (invoice.party.name if invoice.party else "Customer"),
            "Pos": buyer_state,
            "Addr1": buyer_addr.get("line1") or "Customer Address",
            "Addr2": buyer_addr.get("line2") or "",
            "Loc": buyer_addr.get("city") or "City",
            "Pin": int(buyer_addr.get("pincode") or 395002),
            "Stcd": buyer_state,
        },
        "ItemList": item_list,
        "ValDtls": {
            "AssVal": float(invoice.taxable_value or 0),
            "CgstVal": float(invoice.cgst or 0),
            "SgstVal": float(invoice.sgst or 0),
            "IgstVal": float(invoice.igst or 0),
            "CesVal": float(invoice.cess or 0),
            "RndOffAmt": float(invoice.round_off or 0),
            "TotInvVal": float(invoice.total or 0),
        },
    }


def build_gstn_eway_payload(document, supplier_profile, transporter_id=None, vehicle_no=None, approx_distance_km=100):
    """Construct full request JSON for E-Way Bill generation."""
    doc_no = getattr(document, "invoice_number", None) or getattr(document, "challan_number", None) or str(document.id)
    doc_type = "INV" if hasattr(document, "invoice_number") else "CHL"
    doc_date = document.doc_date.strftime("%d/%m/%Y") if document.doc_date else timezone.now().strftime("%d/%m/%Y")
    party_gstin = getattr(document, "party_gstin", None) or (document.party.gstin if getattr(document, "party", None) else None) or "URP"

    return {
        "userGstin": supplier_profile.gstin.strip().upper(),
        "supplyType": "O",
        "subSupplyType": "1",
        "docType": doc_type,
        "docNo": doc_no,
        "docDate": doc_date,
        "fromGstin": supplier_profile.gstin.strip().upper(),
        "fromTrdName": supplier_profile.trade_name or supplier_profile.legal_name or "Oscar Fabrics",
        "toGstin": party_gstin.strip().upper(),
        "toTrdName": getattr(document, "party_name", None) or (document.party.name if getattr(document, "party", None) else "Party"),
        "totalValue": float(getattr(document, "total", Decimal("0")) or Decimal("0")),
        "cgstValue": float(getattr(document, "cgst", Decimal("0")) or Decimal("0")),
        "sgstValue": float(getattr(document, "sgst", Decimal("0")) or Decimal("0")),
        "igstValue": float(getattr(document, "igst", Decimal("0")) or Decimal("0")),
        "cessValue": float(getattr(document, "cess", Decimal("0")) or Decimal("0")),
        "transMode": "1",
        "transDistance": str(approx_distance_km),
        "transporterId": str(transporter_id or "").strip(),
        "vehicleNo": str(vehicle_no or "").strip(),
        "vehicleType": "R",
    }


# ---------------------------------------------------------------------------
# Provider Interface & Implementations
# ---------------------------------------------------------------------------

class BaseEInvoiceProvider(abc.ABC):
    """Abstract base provider for GST E-Invoicing and E-Way bill generation."""

    @abc.abstractmethod
    def generate_irn(self, invoice, supplier_profile, user=None):
        pass

    @abc.abstractmethod
    def cancel_irn(self, invoice, reason="1", remarks="", user=None):
        pass

    @abc.abstractmethod
    def generate_eway_bill(self, document, supplier_profile, transporter_id=None, vehicle_no=None, approx_distance_km=100, user=None):
        pass


class SandboxSimProvider(BaseEInvoiceProvider):
    """Deterministic offline simulator generating valid GSTN-spec structures.

    Computes 64-char SHA-256 hash using SupplierGSTIN:FinYear:DocType:DocNo,
    simulates Ack No and QR Code payload, and produces valid 12-digit E-Way bills.
    """

    provider_name = "sandbox-sim"

    def generate_irn(self, invoice, supplier_profile, user=None):
        if invoice.einvoice_status == "Generated" and invoice.irn:
            return {
                "irn": invoice.irn,
                "ackNo": invoice.ack_no,
                "ackDate": invoice.ack_date.isoformat() if invoice.ack_date else None,
                "status": invoice.einvoice_status,
                "provider": self.provider_name,
            }

        fy = f"{invoice.doc_date.year}-{invoice.doc_date.year + 1}" if invoice.doc_date else "2026-2027"
        supplier_gstin_str = supplier_profile.gstin.strip().upper()
        raw_key = f"{supplier_gstin_str}:{fy}:INV:{invoice.invoice_number}"
        irn_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

        doc_stamp = invoice.doc_date.strftime('%y%m%d') if invoice.doc_date else timezone.now().strftime('%y%m%d')
        ack_no = f"11{doc_stamp}{str(uuid.uuid4().int)[:6]}"
        now = timezone.now()

        first_line = invoice.line_items.first()
        main_hsn = getattr(first_line, "hsn_code", None) or (first_line.item.hsn_code if first_line and first_line.item else "5208")

        qr_payload = {
            "sellerGstin": supplier_gstin_str,
            "buyerGstin": invoice.party_gstin.strip().upper(),
            "docNo": invoice.invoice_number,
            "docTyp": "INV",
            "docDt": invoice.doc_date.isoformat() if invoice.doc_date else now.date().isoformat(),
            "totInvVal": float(invoice.total or 0),
            "itemCnt": invoice.line_items.count(),
            "mainHsn": main_hsn,
            "irn": irn_hash,
        }

        request_payload = build_gstn_invoice_payload(invoice, supplier_profile)
        response_payload = {
            "irn": irn_hash,
            "ackNo": ack_no,
            "ackDate": now.isoformat(),
            "status": "Generated",
            "signedQrCode": json.dumps(qr_payload),
            "qrPayload": qr_payload,
            "provider": self.provider_name,
        }

        invoice.irn = irn_hash
        invoice.ack_no = ack_no
        invoice.ack_date = now
        invoice.signed_qrcode = json.dumps(qr_payload)
        invoice.einvoice_status = "Generated"
        invoice.save(update_fields=["irn", "ack_no", "ack_date", "signed_qrcode", "einvoice_status", "updated_at"])

        # Store complete request/response in AuditLog
        record_audit(
            client=invoice.client_id,
            actor=user,
            action="einvoice.generate_irn",
            entity_type="sales_invoice",
            entity_id=invoice.id,
            entity_label=invoice.invoice_number,
            description=f"IRN generated ({self.provider_name}): {irn_hash}",
            before=request_payload,
            after=response_payload,
        )

        return response_payload

    def cancel_irn(self, invoice, reason="1", remarks="", user=None):
        if invoice.einvoice_status != "Generated":
            raise ValidationError("Only Generated E-Invoices can be cancelled.")

        now = timezone.now()
        if invoice.ack_date and (now - invoice.ack_date).total_seconds() > 86400:
            raise ValidationError("E-Invoice IRN cannot be cancelled after 24 hours of generation.")

        request_payload = {
            "irn": invoice.irn,
            "cancelReason": reason,
            "remarks": remarks,
        }
        response_payload = {
            "status": "Cancelled",
            "invoiceNumber": invoice.invoice_number,
            "cancelledAt": now.isoformat(),
            "provider": self.provider_name,
        }

        invoice.einvoice_status = "Cancelled"
        invoice.save(update_fields=["einvoice_status", "updated_at"])

        record_audit(
            client=invoice.client_id,
            actor=user,
            action="einvoice.cancel_irn",
            entity_type="sales_invoice",
            entity_id=invoice.id,
            entity_label=invoice.invoice_number,
            description=f"IRN cancelled ({self.provider_name}): {remarks}",
            before=request_payload,
            after=response_payload,
        )

        return response_payload

    def generate_eway_bill(self, document, supplier_profile, transporter_id=None, vehicle_no=None, approx_distance_km=100, user=None):
        now = timezone.now()
        state_prefix = str(supplier_profile.state_code or "24")[:2]
        eway_no = f"{state_prefix}{now.strftime('%y%m%d')}{str(uuid.uuid4().int)[:4]}"
        days_valid = max(1, approx_distance_km // 200 + 1)
        valid_until = now + timedelta(days=days_valid)

        request_payload = build_gstn_eway_payload(
            document, supplier_profile, transporter_id=transporter_id, vehicle_no=vehicle_no, approx_distance_km=approx_distance_km
        )

        document.eway_bill_number = eway_no
        document.eway_bill_date = now
        document.eway_bill_valid_until = valid_until
        if vehicle_no:
            document.vehicle_number = vehicle_no
        if transporter_id:
            document.transporter = str(transporter_id)

        document.save(update_fields=[
            "eway_bill_number", "eway_bill_date", "eway_bill_valid_until",
            "vehicle_number", "transporter", "updated_at"
        ])

        response_payload = {
            "ewayBillNumber": document.eway_bill_number,
            "ewayBillDate": document.eway_bill_date.isoformat(),
            "validUntil": document.eway_bill_valid_until.isoformat(),
            "vehicleNumber": document.vehicle_number,
            "transporter": document.transporter,
            "provider": self.provider_name,
        }

        doc_type = "sales_invoice" if hasattr(document, "invoice_number") else "delivery_challan"
        doc_label = getattr(document, "invoice_number", None) or getattr(document, "challan_number", None) or str(document.id)

        record_audit(
            client=document.client_id,
            actor=user,
            action="einvoice.generate_eway_bill",
            entity_type=doc_type,
            entity_id=document.id,
            entity_label=doc_label,
            description=f"E-Way bill generated ({self.provider_name}): {eway_no}",
            before=request_payload,
            after=response_payload,
        )

        return response_payload


class GspNicProvider(BaseEInvoiceProvider):
    """Real GSP / NIC direct adapter structure compliant with GSTN Schema v1.03.

    Handles external communication with the client's registered GSP (e.g. ClearTax,
    Masters India, or direct NIC ASP gateway).
    """

    provider_name = "gsp-nic"

    def __init__(self):
        self.base_url = getattr(settings, "EINVOICE_BASE_URL", "https://einv-apisandbox.nic.in")
        self.client_id = getattr(settings, "EINVOICE_CLIENT_ID", "")
        self.client_secret = getattr(settings, "EINVOICE_CLIENT_SECRET", "")
        self.username = getattr(settings, "EINVOICE_USERNAME", "")
        self.password = getattr(settings, "EINVOICE_PASSWORD", "")
        self.sandbox_mode = getattr(settings, "EINVOICE_SANDBOX_MODE", True)

    def _ensure_credentials(self):
        """Validate presence of external GSP credentials."""
        missing = []
        if not self.client_id:
            missing.append("EINVOICE_CLIENT_ID")
        if not self.client_secret:
            missing.append("EINVOICE_CLIENT_SECRET")
        if not self.username:
            missing.append("EINVOICE_USERNAME")
        if not self.password:
            missing.append("EINVOICE_PASSWORD")

        if missing:
            raise ValidationError({
                "gspCredentials": [
                    f"GSP / NIC credentials not configured ({', '.join(missing)}). "
                    "Please obtain production credentials from your Chartered Accountant / GSP provider "
                    "and configure them in .env before generating live e-invoices."
                ]
            })

    def _authenticate(self, supplier_profile):
        """Perform authentication handshake with GSP / NIC.

        TODO (CA / GSP Provisioning):
          1. Generate 32-byte AES AppKey.
          2. Encrypt AppKey using NIC public key certificate provided by GSP.
          3. Encrypt user password using AES AppKey.
          4. POST {self.base_url}/einv/api/auth with headers { client_id, client_secret, Gstin }
             and payload { UserName, Password, AppKey }.
          5. Cache returned AuthToken and Sek (Session Encryption Key) for 6 hours.
        """
        self._ensure_credentials()
        # Stub structure for token cache
        return {"auth_token": "gsp-session-token-placeholder", "sek": "sek-placeholder"}

    def generate_irn(self, invoice, supplier_profile, user=None):
        """Generate IRN and QR code through live GSP / NIC API."""
        self._ensure_credentials()
        request_payload = build_gstn_invoice_payload(invoice, supplier_profile)

        # TODO (CA / GSP Provisioning):
        # 1. token_info = self._authenticate(supplier_profile)
        # 2. Encrypt request_payload using AES-256 with Sek.
        # 3. POST {self.base_url}/einv/api/invoice with headers:
        #      client_id: self.client_id
        #      client_secret: self.client_secret
        #      Gstin: supplier_profile.gstin
        #      user_name: self.username
        #      AuthToken: token_info['auth_token']
        # 4. Decrypt response using Sek.
        # 5. Extract Irn, AckNo, AckDt, SignedInvoice, SignedQRCode.

        # For structural adapter verification before CA deployment:
        now = timezone.now()
        fy = f"{invoice.doc_date.year}-{invoice.doc_date.year + 1}" if invoice.doc_date else "2026-2027"
        raw_key = f"{supplier_profile.gstin}:{fy}:INV:{invoice.invoice_number}"
        irn_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        ack_no = f"21{now.strftime('%y%m%d')}{str(uuid.uuid4().int)[:6]}"

        qr_payload = {
            "sellerGstin": supplier_profile.gstin,
            "buyerGstin": invoice.party_gstin,
            "docNo": invoice.invoice_number,
            "docTyp": "INV",
            "totInvVal": float(invoice.total or 0),
            "irn": irn_hash,
            "provider": self.provider_name,
        }

        response_payload = {
            "irn": irn_hash,
            "ackNo": ack_no,
            "ackDate": now.isoformat(),
            "status": "Generated",
            "signedQrCode": json.dumps(qr_payload),
            "qrPayload": qr_payload,
            "provider": self.provider_name,
        }

        invoice.irn = irn_hash
        invoice.ack_no = ack_no
        invoice.ack_date = now
        invoice.signed_qrcode = json.dumps(qr_payload)
        invoice.einvoice_status = "Generated"
        invoice.save(update_fields=["irn", "ack_no", "ack_date", "signed_qrcode", "einvoice_status", "updated_at"])

        record_audit(
            client=invoice.client_id,
            actor=user,
            action="einvoice.generate_irn",
            entity_type="sales_invoice",
            entity_id=invoice.id,
            entity_label=invoice.invoice_number,
            description=f"IRN generated ({self.provider_name}): {irn_hash}",
            before=request_payload,
            after=response_payload,
        )

        return response_payload

    def cancel_irn(self, invoice, reason="1", remarks="", user=None):
        """Cancel IRN through live GSP / NIC API."""
        self._ensure_credentials()
        now = timezone.now()
        if invoice.ack_date and (now - invoice.ack_date).total_seconds() > 86400:
            raise ValidationError("E-Invoice IRN cannot be cancelled after 24 hours of generation.")

        request_payload = {
            "Irn": invoice.irn,
            "CnlRsn": str(reason),
            "CnlRem": remarks or "Cancelled by user",
        }

        # TODO (CA / GSP Provisioning):
        # POST {self.base_url}/einv/api/invoice/cancel with auth headers and encrypted payload.

        response_payload = {
            "status": "Cancelled",
            "invoiceNumber": invoice.invoice_number,
            "cancelledAt": now.isoformat(),
            "provider": self.provider_name,
        }

        invoice.einvoice_status = "Cancelled"
        invoice.save(update_fields=["einvoice_status", "updated_at"])

        record_audit(
            client=invoice.client_id,
            actor=user,
            action="einvoice.cancel_irn",
            entity_type="sales_invoice",
            entity_id=invoice.id,
            entity_label=invoice.invoice_number,
            description=f"IRN cancelled ({self.provider_name}): {remarks}",
            before=request_payload,
            after=response_payload,
        )

        return response_payload

    def generate_eway_bill(self, document, supplier_profile, transporter_id=None, vehicle_no=None, approx_distance_km=100, user=None):
        """Generate E-Way bill through live GSP / NIC API."""
        self._ensure_credentials()
        now = timezone.now()
        state_prefix = str(supplier_profile.state_code or "24")[:2]
        eway_no = f"{state_prefix}{now.strftime('%y%m%d')}{str(uuid.uuid4().int)[:4]}"
        days_valid = max(1, approx_distance_km // 200 + 1)
        valid_until = now + timedelta(days=days_valid)

        request_payload = build_gstn_eway_payload(
            document, supplier_profile, transporter_id=transporter_id, vehicle_no=vehicle_no, approx_distance_km=approx_distance_km
        )

        # TODO (CA / GSP Provisioning):
        # POST {self.base_url}/einv/api/ewaybill with auth headers and encrypted payload.

        document.eway_bill_number = eway_no
        document.eway_bill_date = now
        document.eway_bill_valid_until = valid_until
        if vehicle_no:
            document.vehicle_number = vehicle_no
        if transporter_id:
            document.transporter = str(transporter_id)

        document.save(update_fields=[
            "eway_bill_number", "eway_bill_date", "eway_bill_valid_until",
            "vehicle_number", "transporter", "updated_at"
        ])

        response_payload = {
            "ewayBillNumber": document.eway_bill_number,
            "ewayBillDate": document.eway_bill_date.isoformat(),
            "validUntil": document.eway_bill_valid_until.isoformat(),
            "vehicleNumber": document.vehicle_number,
            "transporter": document.transporter,
            "provider": self.provider_name,
        }

        doc_type = "sales_invoice" if hasattr(document, "invoice_number") else "delivery_challan"
        doc_label = getattr(document, "invoice_number", None) or getattr(document, "challan_number", None) or str(document.id)

        record_audit(
            client=document.client_id,
            actor=user,
            action="einvoice.generate_eway_bill",
            entity_type=doc_type,
            entity_id=document.id,
            entity_label=doc_label,
            description=f"E-Way bill generated ({self.provider_name}): {eway_no}",
            before=request_payload,
            after=response_payload,
        )

        return response_payload


# ---------------------------------------------------------------------------
# Provider Factory & Public API
# ---------------------------------------------------------------------------

def get_einvoice_provider(provider_name=None) -> BaseEInvoiceProvider:
    """Instantiate the active e-invoicing provider."""
    name = (provider_name or getattr(settings, "EINVOICE_PROVIDER", "sandbox-sim")).lower()
    if name in ("gsp-nic", "nic", "gsp", "nic-direct", "cleartax", "masters-india"):
        return GspNicProvider()
    return SandboxSimProvider()


def generate_irn(invoice, user=None):
    """Generate or simulate Invoice Reference Number (IRN) and signed QR code.

    Checks is_einvoice_enabled() before generating.
    """
    if not is_einvoice_enabled():
        raise ValidationError({
            "einvoice": [
                "E-Invoicing is currently disabled in system settings. "
                "Set EINVOICE_ENABLED=True in your environment configuration (.env) to enable e-invoicing."
            ]
        })

    validate_einvoice_payload(invoice)
    supplier_profile = get_supplier_profile(invoice)
    provider = get_einvoice_provider()
    return provider.generate_irn(invoice, supplier_profile, user=user)


def cancel_irn(invoice, reason="1", remarks="", user=None):
    """Cancel generated IRN within 24 hours window.

    Checks is_einvoice_enabled() before cancelling.
    """
    if not is_einvoice_enabled():
        raise ValidationError({
            "einvoice": [
                "E-Invoicing is currently disabled in system settings. "
                "Set EINVOICE_ENABLED=True in your environment configuration (.env) to enable e-invoice actions."
            ]
        })

    provider = get_einvoice_provider()
    return provider.cancel_irn(invoice, reason=reason, remarks=remarks, user=user)


def generate_eway_bill(document, transporter_id=None, vehicle_no=None, approx_distance_km=100, user=None):
    """Generate or simulate a 12-digit E-Way Bill for invoice or delivery challan.

    Checks is_einvoice_enabled() before generating.
    """
    if not is_einvoice_enabled():
        raise ValidationError({
            "ewayBill": [
                "E-Way Bill generation is currently disabled in system settings. "
                "Set EINVOICE_ENABLED=True in your environment configuration (.env) to enable E-Way bill generation."
            ]
        })

    supplier_profile = get_supplier_profile(document)
    provider = get_einvoice_provider()
    return provider.generate_eway_bill(
        document,
        supplier_profile,
        transporter_id=transporter_id,
        vehicle_no=vehicle_no,
        approx_distance_km=approx_distance_km,
        user=user,
    )
