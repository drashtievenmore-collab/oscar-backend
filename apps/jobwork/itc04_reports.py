"""ITC-04 Statutory Quarterly Job-Work Reporting (PDF §20).

GST ITC-04 is the quarterly statement filed by registered manufacturers
sending inputs or capital goods to job workers.

Table 4: Details of inputs/capital goods sent for job work.
Table 5A: Details of inputs/capital goods received back from job workers.
"""
import csv
import io
from datetime import date, datetime
from decimal import Decimal
from django.db.models import Sum, Q

from apps.core.money import ZERO, round2, round4, D
from .models import JobWorkOrder, JWOOutward, JWOInward, JWOLossEntry


def parse_quarter_dates(financial_year=None, quarter=None, start_date=None, end_date=None):
    """Resolve start_date and end_date from quarter and financial year, or explicit dates.

    Indian Financial Year starts April 1 and ends March 31 of next calendar year.
    Examples:
        financial_year="2026-27", quarter="Q1" -> 2026-04-01 to 2026-06-30
        financial_year="2026-27", quarter="Q2" -> 2026-07-01 to 2026-09-30
        financial_year="2026-27", quarter="Q3" -> 2026-10-01 to 2026-12-31
        financial_year="2026-27", quarter="Q4" -> 2027-01-01 to 2027-03-31
    """
    if start_date and end_date:
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        return start_date, end_date

    today = date.today()
    # Default financial year calculation
    if not financial_year:
        if today.month >= 4:
            base_year = today.year
        else:
            base_year = today.year - 1
        financial_year = f"{base_year}-{str(base_year + 1)[-2:]}"
    else:
        parts = financial_year.split("-")
        base_year = int(parts[0])

    if not quarter:
        # Determine current quarter
        if today.month in [4, 5, 6]:
            quarter = "Q1"
        elif today.month in [7, 8, 9]:
            quarter = "Q2"
        elif today.month in [10, 11, 12]:
            quarter = "Q3"
        else:
            quarter = "Q4"

    q_upper = quarter.upper().strip()
    if q_upper == "Q1":
        s_date = date(base_year, 4, 1)
        e_date = date(base_year, 6, 30)
    elif q_upper == "Q2":
        s_date = date(base_year, 7, 1)
        e_date = date(base_year, 9, 30)
    elif q_upper == "Q3":
        s_date = date(base_year, 10, 1)
        e_date = date(base_year, 12, 31)
    elif q_upper == "Q4":
        s_date = date(base_year + 1, 1, 1)
        e_date = date(base_year + 1, 3, 31)
    else:
        # Full FY fallback
        s_date = date(base_year, 4, 1)
        e_date = date(base_year + 1, 3, 31)

    return s_date, e_date


def get_itc04_report_data(client, financial_year=None, quarter=None, start_date=None, end_date=None, vendor_id=None):
    """Compile Table 4 and Table 5A records and reconciliation totals for ITC-04."""
    s_date, e_date = parse_quarter_dates(financial_year, quarter, start_date, end_date)

    # 1. Table 4: Goods sent for job work
    outward_qs = JWOOutward.objects.filter(
        client=client,
        deleted_at__isnull=True,
        date__gte=s_date,
        date__lte=e_date,
    ).select_related("job_work_order", "job_work_order__vendor").prefetch_related("takas")

    if vendor_id:
        outward_qs = outward_qs.filter(job_work_order__vendor_id=vendor_id)

    table_4_rows = []
    total_sent_meters = Decimal("0")
    total_sent_takas = 0
    total_sent_value = Decimal("0")

    for out in outward_qs:
        jwo = out.job_work_order
        v = jwo.vendor
        v_name = v.name if v else (jwo.vendor_name or "Unknown Vendor")
        v_gstin = v.gstin if v and v.gstin else ""
        v_state = v.place_of_supply if v and v.place_of_supply else ""

        takas = list(out.takas.all())
        taka_count = len(takas)
        taka_weight = sum((t.weight_kg or Decimal("0") for t in takas), Decimal("0"))
        meters = out.qty or sum((t.meters or Decimal("0") for t in takas), Decimal("0"))
        rate = jwo.rate or Decimal("0")
        taxable_val = round2(meters * (rate if rate > 0 else Decimal("50.00")))

        total_sent_meters += meters
        total_sent_takas += taka_count
        total_sent_value += taxable_val

        table_4_rows.append({
            "id": str(out.id),
            "challanNumber": out.challan_no or out.no or f"DC-{out.id}",
            "challanDate": out.date.isoformat(),
            "jwoNumber": jwo.jwo_no,
            "vendorName": v_name,
            "vendorGstin": v_gstin,
            "state": v_state,
            "goodsType": "Inputs (Fabric)",
            "process": jwo.process or "Processing",
            "uom": "MTR",
            "quantity": float(meters),
            "takaCount": taka_count,
            "weightKg": float(taka_weight),
            "taxableValue": float(taxable_val),
            "remarks": out.remarks or "",
        })

    # 2. Table 5A: Goods received back from job work
    inward_qs = JWOInward.objects.filter(
        client=client,
        deleted_at__isnull=True,
        date__gte=s_date,
        date__lte=e_date,
    ).select_related("job_work_order", "job_work_order__vendor").prefetch_related("takas", "job_work_order__outwards", "job_work_order__loss_entries")

    if vendor_id:
        inward_qs = inward_qs.filter(job_work_order__vendor_id=vendor_id)

    table_5a_rows = []
    total_received_meters = Decimal("0")
    total_received_takas = 0
    total_loss_meters = Decimal("0")

    for inw in inward_qs:
        jwo = inw.job_work_order
        v = jwo.vendor
        v_name = v.name if v else (jwo.vendor_name or "Unknown Vendor")
        v_gstin = v.gstin if v and v.gstin else ""

        # Find linked outward challan
        primary_out = jwo.outwards.filter(deleted_at__isnull=True).order_by("date").first()
        orig_challan_no = primary_out.challan_no or primary_out.no if primary_out else ""
        orig_challan_date = primary_out.date.isoformat() if primary_out else ""

        takas = list(inw.takas.all())
        taka_count = len(takas)
        taka_weight = sum((t.weight_kg or Decimal("0") for t in takas), Decimal("0"))
        meters_received = inw.qty or sum((t.meters or Decimal("0") for t in takas), Decimal("0"))
        accepted = inw.accepted if inw.accepted is not None else meters_received
        rejected = inw.rejected or Decimal("0")

        # Loss entries approved on JWO
        losses = sum(
            (l.qty for l in jwo.loss_entries.filter(status="Approved", deleted_at__isnull=True)),
            Decimal("0"),
        )

        total_received_meters += meters_received
        total_received_takas += taka_count
        total_loss_meters += losses

        table_5a_rows.append({
            "id": str(inw.id),
            "inwardChallanNumber": inw.challan_no or inw.no or f"RCV-{inw.id}",
            "inwardDate": inw.date.isoformat(),
            "originalChallanNumber": orig_challan_no,
            "originalChallanDate": orig_challan_date,
            "jwoNumber": jwo.jwo_no,
            "vendorName": v_name,
            "vendorGstin": v_gstin,
            "goodsType": "Processed Fabric",
            "process": jwo.process or "Processing",
            "uom": "MTR",
            "quantityReceived": float(meters_received),
            "quantityAccepted": float(accepted),
            "quantityRejected": float(rejected),
            "takaCount": taka_count,
            "weightKg": float(taka_weight),
            "approvedLoss": float(losses),
            "status": inw.status,
        })

    pending_meters = max(Decimal("0"), total_sent_meters - total_received_meters - total_loss_meters)

    return {
        "period": {
            "financialYear": financial_year,
            "quarter": quarter,
            "startDate": s_date.isoformat(),
            "endDate": e_date.isoformat(),
        },
        "summary": {
            "totalSentMeters": float(total_sent_meters),
            "totalSentTakas": total_sent_takas,
            "totalSentValue": float(total_sent_value),
            "totalReceivedMeters": float(total_received_meters),
            "totalReceivedTakas": total_received_takas,
            "totalLossMeters": float(total_loss_meters),
            "pendingWithProcessorsMeters": float(pending_meters),
        },
        "table4": table_4_rows,
        "table5a": table_5a_rows,
    }


def generate_itc04_csv(report_data, table="both"):
    """Render ITC-04 data into CSV formatted according to GST specifications."""
    output = io.StringIO()
    writer = csv.writer(output)

    period = report_data.get("period", {})
    writer.writerow(["FORM GST ITC-04"])
    writer.writerow(["Financial Year", period.get("financialYear"), "Quarter", period.get("quarter")])
    writer.writerow(["Period", f"{period.get('startDate')} to {period.get('endDate')}"])
    writer.writerow([])

    if table in ("both", "table4"):
        writer.writerow(["--- TABLE 4: DETAILS OF INPUTS SENT FOR JOB WORK ---"])
        writer.writerow([
            "Challan Number",
            "Challan Date",
            "JWO Number",
            "Job Worker GSTIN",
            "Job Worker Name",
            "State",
            "Description of Goods",
            "Process",
            "UOM",
            "Quantity (Meters)",
            "Takas",
            "Weight (Kg)",
            "Taxable Value (INR)",
        ])
        for r in report_data.get("table4", []):
            writer.writerow([
                r["challanNumber"],
                r["challanDate"],
                r["jwoNumber"],
                r["vendorGstin"],
                r["vendorName"],
                r["state"],
                r["goodsType"],
                r["process"],
                r["uom"],
                r["quantity"],
                r["takaCount"],
                r["weightKg"],
                r["taxableValue"],
            ])
        writer.writerow([])

    if table in ("both", "table5a"):
        writer.writerow(["--- TABLE 5A: DETAILS OF INPUTS RECEIVED BACK FROM JOB WORK ---"])
        writer.writerow([
            "Inward Challan No",
            "Inward Date",
            "Original Challan No",
            "Original Challan Date",
            "JWO Number",
            "Job Worker GSTIN",
            "Job Worker Name",
            "Description of Goods",
            "UOM",
            "Quantity Received (Meters)",
            "Accepted",
            "Rejected",
            "Takas",
            "Weight (Kg)",
            "Approved Loss (Meters)",
            "Status",
        ])
        for r in report_data.get("table5a", []):
            writer.writerow([
                r["inwardChallanNumber"],
                r["inwardDate"],
                r["originalChallanNumber"],
                r["originalChallanDate"],
                r["jwoNumber"],
                r["vendorGstin"],
                r["vendorName"],
                r["goodsType"],
                r["uom"],
                r["quantityReceived"],
                r["quantityAccepted"],
                r["quantityRejected"],
                r["takaCount"],
                r["weightKg"],
                r["approvedLoss"],
                r["status"],
            ])

    return output.getvalue()
