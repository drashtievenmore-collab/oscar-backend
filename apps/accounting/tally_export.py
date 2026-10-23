"""Tally XML and Excel Export Services (PDF §14).

Exports masters (Ledgers) and vouchers (Sales, Purchase, Receipt, Payment,
Journal, Credit/Debit Note) in standard Tally XML format, as well as CSV/Excel exports.
"""
import io
import csv
from datetime import date
from decimal import Decimal
import xml.etree.ElementTree as ET
from xml.dom import minidom

from django.utils import timezone

from apps.core.money import ZERO, D, round2
from .models import Account, JournalEntry, JournalLine, StandaloneNote
from apps.masters.models import Party
from apps.purchase.models import PaymentOut, PurchaseBill
from apps.sales.models import PaymentIn, SalesInvoice


def format_tally_date(d):
    """Tally date format: YYYYMMDD."""
    if not d:
        return ""
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return d.strftime("%Y%m%d")


def export_tally_masters_xml(client_id, company_name="Oscar ERP"):
    """Export chart of accounts and parties as Tally Ledger masters."""
    root = ET.Element("ENVELOPE")
    header = ET.SubElement(root, "HEADER")
    ET.SubElement(header, "TALLYREQUEST").text = "Import Data"

    body = ET.SubElement(root, "BODY")
    import_data = ET.SubElement(body, "IMPORTDATA")
    req_desc = ET.SubElement(import_data, "REQUESTDESC")
    ET.SubElement(req_desc, "REPORTNAME").text = "All Masters"
    static_vars = ET.SubElement(req_desc, "STATICVARIABLES")
    ET.SubElement(static_vars, "SVCURRENTCOMPANY").text = company_name

    req_data = ET.SubElement(import_data, "REQUESTDATA")

    # 1. Chart of Accounts
    accounts = Account.objects.filter(client_id=client_id, deleted_at__isnull=True)
    for acc in accounts:
        msg = ET.SubElement(req_data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
        ledger_el = ET.SubElement(msg, "LEDGER", {"NAME": acc.name, "ACTION": "Create"})
        ET.SubElement(ledger_el, "NAME").text = acc.name
        parent_name = "Primary"
        if acc.parent:
            parent_name = acc.parent.name
        elif acc.type == "Asset":
            parent_name = "Current Assets" if acc.subtype != "Bank" else "Bank Accounts"
        elif acc.type == "Liability":
            parent_name = "Current Liabilities"
        elif acc.type == "Income":
            parent_name = "Sales Accounts"
        elif acc.type == "Expense":
            parent_name = "Direct Expenses"
        ET.SubElement(ledger_el, "PARENT").text = parent_name
        ET.SubElement(ledger_el, "OPENINGBALANCE").text = str(acc.opening_balance or 0)

    # 2. Parties
    parties = Party.objects.filter(client_id=client_id, deleted_at__isnull=True)
    for party in parties:
        msg = ET.SubElement(req_data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
        ledger_el = ET.SubElement(msg, "LEDGER", {"NAME": party.name, "ACTION": "Create"})
        ET.SubElement(ledger_el, "NAME").text = party.name
        parent_name = "Sundry Debtors" if party.type in ("Customer", "Both") else "Sundry Creditors"
        ET.SubElement(ledger_el, "PARENT").text = parent_name
        if party.gstin:
            ET.SubElement(ledger_el, "PARTYGSTIN").text = party.gstin
        if getattr(party, "pan", None):
            ET.SubElement(ledger_el, "PANNUMBER").text = party.pan
        if getattr(party, "state", None):
            ET.SubElement(ledger_el, "STATENAME").text = party.state

    rough_string = ET.tostring(root, "utf-8")
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ")


def export_tally_vouchers_xml(client_id, date_from=None, date_to=None, company_name="Oscar ERP"):
    """Export transactions as Tally Vouchers (Sales, Purchase, Receipt, Payment, Journal, Notes)."""
    root = ET.Element("ENVELOPE")
    header = ET.SubElement(root, "HEADER")
    ET.SubElement(header, "TALLYREQUEST").text = "Import Data"

    body = ET.SubElement(root, "BODY")
    import_data = ET.SubElement(body, "IMPORTDATA")
    req_desc = ET.SubElement(import_data, "REQUESTDESC")
    ET.SubElement(req_desc, "REPORTNAME").text = "Vouchers"
    static_vars = ET.SubElement(req_desc, "STATICVARIABLES")
    ET.SubElement(static_vars, "SVCURRENTCOMPANY").text = company_name

    req_data = ET.SubElement(import_data, "REQUESTDATA")

    # 1. Sales Invoices
    sales = SalesInvoice.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).exclude(status__in=["Draft", "Cancelled"]).select_related("party")
    if date_from:
        sales = sales.filter(doc_date__gte=date_from)
    if date_to:
        sales = sales.filter(doc_date__lte=date_to)

    for inv in sales:
        msg = ET.SubElement(req_data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
        vch = ET.SubElement(msg, "VOUCHER", {"VCHTYPE": "Sales", "ACTION": "Create"})
        ET.SubElement(vch, "DATE").text = format_tally_date(inv.doc_date)
        ET.SubElement(vch, "VOUCHERNUMBER").text = inv.invoice_number or ""
        party_name = inv.party_name or (inv.party.name if inv.party else "Cash Customer")
        ET.SubElement(vch, "PARTYLEDGERNAME").text = party_name
        ET.SubElement(vch, "NARRATION").text = f"Sales invoice {inv.invoice_number}"

        # Party line (Dr -> negative in Tally XML)
        pl = ET.SubElement(vch, "ALLLEDGERENTRIES.LIST")
        ET.SubElement(pl, "LEDGERNAME").text = party_name
        ET.SubElement(pl, "ISDEEMEDPOSITIVE").text = "Yes"
        ET.SubElement(pl, "AMOUNT").text = f"-{inv.total}"

        # Sales line (Cr -> positive in Tally XML)
        sl = ET.SubElement(vch, "ALLLEDGERENTRIES.LIST")
        ET.SubElement(sl, "LEDGERNAME").text = "Sales"
        ET.SubElement(sl, "ISDEEMEDPOSITIVE").text = "No"
        ET.SubElement(sl, "AMOUNT").text = f"{inv.taxable_value or (inv.subtotal - inv.total_discount)}"

        # Tax lines
        if inv.cgst and D(inv.cgst) > ZERO:
            tl = ET.SubElement(vch, "ALLLEDGERENTRIES.LIST")
            ET.SubElement(tl, "LEDGERNAME").text = "Output CGST"
            ET.SubElement(tl, "ISDEEMEDPOSITIVE").text = "No"
            ET.SubElement(tl, "AMOUNT").text = f"{inv.cgst}"
        if inv.sgst and D(inv.sgst) > ZERO:
            tl = ET.SubElement(vch, "ALLLEDGERENTRIES.LIST")
            ET.SubElement(tl, "LEDGERNAME").text = "Output SGST"
            ET.SubElement(tl, "ISDEEMEDPOSITIVE").text = "No"
            ET.SubElement(tl, "AMOUNT").text = f"{inv.sgst}"
        if inv.igst and D(inv.igst) > ZERO:
            tl = ET.SubElement(vch, "ALLLEDGERENTRIES.LIST")
            ET.SubElement(tl, "LEDGERNAME").text = "Output IGST"
            ET.SubElement(tl, "ISDEEMEDPOSITIVE").text = "No"
            ET.SubElement(tl, "AMOUNT").text = f"{inv.igst}"

    # 2. Purchase Bills
    bills = PurchaseBill.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).exclude(status__in=["Draft", "Cancelled"]).select_related("party")
    if date_from:
        bills = bills.filter(doc_date__gte=date_from)
    if date_to:
        bills = bills.filter(doc_date__lte=date_to)

    for bill in bills:
        msg = ET.SubElement(req_data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
        vch = ET.SubElement(msg, "VOUCHER", {"VCHTYPE": "Purchase", "ACTION": "Create"})
        ET.SubElement(vch, "DATE").text = format_tally_date(bill.doc_date)
        ET.SubElement(vch, "VOUCHERNUMBER").text = bill.bill_number or ""
        vendor_name = bill.party_name or (bill.party.name if bill.party else "Cash Vendor")
        ET.SubElement(vch, "PARTYLEDGERNAME").text = vendor_name
        ET.SubElement(vch, "NARRATION").text = f"Purchase bill {bill.bill_number}"

        # Purchase line (Dr)
        pl = ET.SubElement(vch, "ALLLEDGERENTRIES.LIST")
        ET.SubElement(pl, "LEDGERNAME").text = "Purchases"
        ET.SubElement(pl, "ISDEEMEDPOSITIVE").text = "Yes"
        ET.SubElement(pl, "AMOUNT").text = f"-{bill.taxable_value or (bill.subtotal - bill.total_discount)}"

        # Vendor line (Cr)
        vl = ET.SubElement(vch, "ALLLEDGERENTRIES.LIST")
        ET.SubElement(vl, "LEDGERNAME").text = vendor_name
        ET.SubElement(vl, "ISDEEMEDPOSITIVE").text = "No"
        ET.SubElement(vl, "AMOUNT").text = f"{bill.total}"

    # 3. Payments In (Receipts)
    payments_in = PaymentIn.objects.filter(
        client_id=client_id, deleted_at__isnull=True, status="Active"
    ).select_related("party", "bank_account")
    if date_from:
        payments_in = payments_in.filter(payment_date__gte=date_from)
    if date_to:
        payments_in = payments_in.filter(payment_date__lte=date_to)

    for pin in payments_in:
        msg = ET.SubElement(req_data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
        vch = ET.SubElement(msg, "VOUCHER", {"VCHTYPE": "Receipt", "ACTION": "Create"})
        ET.SubElement(vch, "DATE").text = format_tally_date(pin.payment_date)
        ET.SubElement(vch, "VOUCHERNUMBER").text = pin.payment_number
        party_name = pin.party.name if pin.party else "Customer"
        bank_name = pin.bank_account.name if pin.bank_account else "Bank Accounts"
        ET.SubElement(vch, "PARTYLEDGERNAME").text = party_name

        # Bank (Dr)
        bl = ET.SubElement(vch, "ALLLEDGERENTRIES.LIST")
        ET.SubElement(bl, "LEDGERNAME").text = bank_name
        ET.SubElement(bl, "ISDEEMEDPOSITIVE").text = "Yes"
        ET.SubElement(bl, "AMOUNT").text = f"-{pin.amount}"

        # Party (Cr)
        pl = ET.SubElement(vch, "ALLLEDGERENTRIES.LIST")
        ET.SubElement(pl, "LEDGERNAME").text = party_name
        ET.SubElement(pl, "ISDEEMEDPOSITIVE").text = "No"
        ET.SubElement(pl, "AMOUNT").text = f"{pin.amount}"

    # 4. Payments Out
    payments_out = PaymentOut.objects.filter(
        client_id=client_id, deleted_at__isnull=True, status="Active"
    ).select_related("party", "bank_account")
    if date_from:
        payments_out = payments_out.filter(payment_date__gte=date_from)
    if date_to:
        payments_out = payments_out.filter(payment_date__lte=date_to)

    for pout in payments_out:
        msg = ET.SubElement(req_data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
        vch = ET.SubElement(msg, "VOUCHER", {"VCHTYPE": "Payment", "ACTION": "Create"})
        ET.SubElement(vch, "DATE").text = format_tally_date(pout.payment_date)
        ET.SubElement(vch, "VOUCHERNUMBER").text = pout.payment_number
        vendor_name = pout.party.name if pout.party else "Vendor"
        bank_name = pout.bank_account.name if pout.bank_account else "Bank Accounts"
        ET.SubElement(vch, "PARTYLEDGERNAME").text = vendor_name

        # Party (Dr)
        pl = ET.SubElement(vch, "ALLLEDGERENTRIES.LIST")
        ET.SubElement(pl, "LEDGERNAME").text = vendor_name
        ET.SubElement(pl, "ISDEEMEDPOSITIVE").text = "Yes"
        ET.SubElement(pl, "AMOUNT").text = f"-{pout.amount}"

        # Bank (Cr)
        bl = ET.SubElement(vch, "ALLLEDGERENTRIES.LIST")
        ET.SubElement(bl, "LEDGERNAME").text = bank_name
        ET.SubElement(bl, "ISDEEMEDPOSITIVE").text = "No"
        ET.SubElement(bl, "AMOUNT").text = f"{pout.amount}"

    rough_string = ET.tostring(root, "utf-8")
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ")


def export_register_csv(client_id, register_type="sales", date_from=None, date_to=None):
    """Generate CSV string for any financial or transactional register."""
    output = io.StringIO()
    writer = csv.writer(output)

    if register_type == "sales":
        writer.writerow(["Invoice Number", "Date", "Customer", "GSTIN", "Taxable Value", "CGST", "SGST", "IGST", "Total", "Status"])
        qs = SalesInvoice.objects.filter(client_id=client_id, deleted_at__isnull=True).exclude(status__in=["Draft", "Cancelled"]).order_by("doc_date")
        if date_from: qs = qs.filter(doc_date__gte=date_from)
        if date_to: qs = qs.filter(doc_date__lte=date_to)
        for row in qs:
            writer.writerow([row.invoice_number, row.doc_date, row.party_name, row.party_gstin, row.taxable_value, row.cgst, row.sgst, row.igst, row.total, row.status])

    elif register_type == "purchase":
        writer.writerow(["Bill Number", "Date", "Vendor", "GSTIN", "Taxable Value", "CGST", "SGST", "IGST", "Total", "Status"])
        qs = PurchaseBill.objects.filter(client_id=client_id, deleted_at__isnull=True).exclude(status__in=["Draft", "Cancelled"]).order_by("doc_date")
        if date_from: qs = qs.filter(doc_date__gte=date_from)
        if date_to: qs = qs.filter(doc_date__lte=date_to)
        for row in qs:
            writer.writerow([row.bill_number, row.doc_date, row.party_name, row.party_gstin, row.taxable_value, row.cgst, row.sgst, row.igst, row.total, row.status])

    elif register_type == "standalone_notes":
        writer.writerow(["Note Number", "Type", "Date", "Party", "Against Doc", "Taxable", "Total", "Status"])
        qs = StandaloneNote.objects.filter(client_id=client_id, deleted_at__isnull=True).order_by("note_date")
        if date_from: qs = qs.filter(note_date__gte=date_from)
        if date_to: qs = qs.filter(note_date__lte=date_to)
        for row in qs:
            writer.writerow([row.note_number, row.note_type, row.note_date, row.party.name if row.party else "", row.against_document_number, row.taxable_amount, row.total_amount, row.status])

    return output.getvalue()
