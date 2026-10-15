"""
Reports, dashboards and exports (api.md §12).

The five figures the ``/reports`` screen computes client-side today
(inventory valuation, customer revenue, vendor allocation, AR/AP ageing, P&L)
are the first five keys here.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounting import reports as financial
from apps.core.exceptions import NotFound, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission
from apps.inventory import services as stock

MONEY = DecimalField(max_digits=18, decimal_places=2)


def money_sum(field, **kwargs):
    return Coalesce(Sum(field, **kwargs), Value(Decimal("0.00")), output_field=MONEY)


#: api.md §12 -- the catalogue, with the parameters each report accepts.
REPORT_CATALOGUE = [
    ("inventory-valuation", "Inventory Valuation", "Inventory", ["asOf", "method"]),
    ("stock-summary", "Stock Summary", "Inventory", []),
    ("stock-movement", "Stock Movement", "Inventory", ["date_from", "date_to", "itemId"]),
    ("customer-revenue", "Customer Revenue", "Sales", ["date_from", "date_to"]),
    ("sales-register", "Sales Register", "Sales", ["date_from", "date_to"]),
    ("vendor-allocation", "Vendor Allocation", "Purchase", ["date_from", "date_to"]),
    ("purchase-register", "Purchase Register", "Purchase", ["date_from", "date_to"]),
    ("ar-ap-ageing", "AR / AP Ageing", "Accounts", ["type", "asOf"]),
    ("profit-and-loss", "Profit and Loss", "Accounts", ["date_from", "date_to"]),
    ("balance-sheet", "Balance Sheet", "Accounts", ["asOf"]),
    ("gst-summary", "GST Summary", "Accounts", ["date_from", "date_to"]),
    ("crm-pipeline", "CRM Pipeline", "CRM", ["date_from", "date_to"]),
    ("crm-conversion", "CRM Conversion", "CRM", ["date_from", "date_to"]),
    ("on-time-velocity", "On-time Velocity", "PMS", ["date_from", "date_to"]),
    ("stage-bottleneck", "Stage Bottleneck", "PMS", []),
    ("delay-reason-pareto", "Delay Reason Pareto", "PMS", ["date_from", "date_to"]),
    ("department-efficiency", "Department Efficiency", "PMS", []),
    ("hrms-attendance", "Attendance Report", "HRMS", ["month", "departmentId"]),
    ("hrms-payroll-summary", "Payroll Summary", "HRMS", ["month"]),
    ("hrms-attrition", "Attrition", "HRMS", ["date_from", "date_to"]),
]


class ReportCatalogueView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_financial_reports"]

    def get(self, request):
        return Response(
            envelope(
                [
                    {"key": key, "name": name, "module": module, "parameters": params}
                    for key, name, module, params in REPORT_CATALOGUE
                ]
            )
        )


class ReportView(APIView):
    """``GET /reports/{reportKey}/``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_financial_reports"]

    def get(self, request, report_key):
        client_id = request.client_id
        params = request.query_params
        date_from = params.get("date_from")
        date_to = params.get("date_to")
        as_of = params.get("asOf") or params.get("as_of")

        handler = getattr(self, f"_report_{report_key.replace('-', '_')}", None)
        if handler is not None:
            return handler(request, client_id, date_from, date_to, as_of)

        # Accounts, CRM and PMS reports are owned by their modules.
        if report_key in (
            "profit-and-loss", "balance-sheet", "gst-summary", "cash-flow", "trial-balance",
        ):
            from apps.accounting.views import FinancialReportView

            return FinancialReportView().get(request, report_key)

        from apps.pms import services as pms_services

        if report_key in pms_services.PMS_REPORTS:
            from apps.pms.views import PmsReportView

            return PmsReportView().get(request, report_key)

        if report_key in ("crm-pipeline", "crm-conversion"):
            from apps.crm.views import CrmReportView

            return CrmReportView().get(request, report_key)

        raise NotFound(f"Unknown report '{report_key}'.")

    # -- the five the frontend computes today -------------------------------
    def _report_inventory_valuation(self, request, client_id, *args):
        from apps.inventory.views import ValuationView

        return ValuationView().get(request)

    def _report_customer_revenue(self, request, client_id, date_from, date_to, as_of):
        """Revenue concentration by customer from recognised invoices."""
        from apps.sales.models import SalesInvoice

        queryset = SalesInvoice.objects.filter(
            client_id=client_id, deleted_at__isnull=True, posted_at__isnull=False
        ).exclude(status="Cancelled")
        if date_from:
            queryset = queryset.filter(doc_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(doc_date__lte=date_to)

        rows = (
            queryset.values("party_id", "party_name")
            .annotate(
                revenue=money_sum("taxable_value"),
                invoiced=money_sum("total"),
                received=money_sum("amount_paid"),
                invoiceCount=Count("id"),
            )
            .order_by("-revenue")
        )
        rows = list(rows)
        total = round2(sum((row["revenue"] for row in rows), ZERO)) or Decimal("1")
        return Response(
            envelope(
                [
                    {
                        "customerId": str(row["party_id"]),
                        "customerName": row["party_name"],
                        "revenue": round2(row["revenue"]),
                        "invoiced": round2(row["invoiced"]),
                        "received": round2(row["received"]),
                        "outstanding": round2(row["invoiced"] - row["received"]),
                        "invoiceCount": row["invoiceCount"],
                        "sharePct": round(float(row["revenue"] / total * 100), 1),
                    }
                    for row in rows
                ],
                aggregates={"totalRevenue": round2(sum((r["revenue"] for r in rows), ZERO))},
            )
        )

    def _report_vendor_allocation(self, request, client_id, date_from, date_to, as_of):
        """Procurement spend by vendor."""
        from apps.purchase.models import PurchaseBill

        queryset = PurchaseBill.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).exclude(status__in=["Cancelled", "Draft"])
        if date_from:
            queryset = queryset.filter(doc_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(doc_date__lte=date_to)

        rows = list(
            queryset.values("party_id", "party_name")
            .annotate(
                spend=money_sum("total"),
                paid=money_sum("amount_paid"),
                billCount=Count("id"),
            )
            .order_by("-spend")
        )
        total = round2(sum((row["spend"] for row in rows), ZERO)) or Decimal("1")
        return Response(
            envelope(
                [
                    {
                        "vendorId": str(row["party_id"]),
                        "vendorName": row["party_name"],
                        "spend": round2(row["spend"]),
                        "paid": round2(row["paid"]),
                        "outstanding": round2(row["spend"] - row["paid"]),
                        "billCount": row["billCount"],
                        "sharePct": round(float(row["spend"] / total * 100), 1),
                    }
                    for row in rows
                ],
                aggregates={"totalSpend": round2(sum((r["spend"] for r in rows), ZERO))},
            )
        )

    def _report_ar_ap_ageing(self, request, client_id, date_from, date_to, as_of):
        kind = request.query_params.get("type") or "receivable"
        return Response(financial.ageing(client_id, kind, as_of))

    def _report_sales_register(self, request, client_id, date_from, date_to, as_of):
        from apps.sales.models import SalesInvoice

        queryset = SalesInvoice.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).exclude(status="Draft").order_by("doc_date")
        if date_from:
            queryset = queryset.filter(doc_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(doc_date__lte=date_to)

        rows = [
            {
                "id": str(invoice.id),
                "invoiceNumber": invoice.invoice_number,
                "date": invoice.doc_date,
                "customerName": invoice.party_name,
                "gstin": invoice.party_gstin,
                "placeOfSupply": invoice.place_of_supply,
                "taxableValue": round2(invoice.taxable_value),
                "cgst": round2(invoice.cgst),
                "sgst": round2(invoice.sgst),
                "igst": round2(invoice.igst),
                "total": round2(invoice.total),
                "status": invoice.status,
            }
            for invoice in queryset
        ]
        return Response(
            envelope(
                rows,
                aggregates={
                    "count": len(rows),
                    "taxableValue": round2(sum((r["taxableValue"] for r in rows), ZERO)),
                    "total": round2(sum((r["total"] for r in rows), ZERO)),
                },
            )
        )

    def _report_purchase_register(self, request, client_id, date_from, date_to, as_of):
        from apps.purchase.models import PurchaseBill

        queryset = PurchaseBill.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).exclude(status="Draft").order_by("doc_date")
        if date_from:
            queryset = queryset.filter(doc_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(doc_date__lte=date_to)

        rows = [
            {
                "id": str(bill.id),
                "billNumber": bill.bill_number,
                "vendorBillNumber": bill.vendor_bill_number,
                "date": bill.doc_date,
                "vendorName": bill.party_name,
                "taxableValue": round2(bill.taxable_value),
                "total": round2(bill.total),
                "status": bill.status,
                "qcStatus": bill.qc_status,
            }
            for bill in queryset
        ]
        return Response(
            envelope(
                rows,
                aggregates={
                    "count": len(rows),
                    "total": round2(sum((r["total"] for r in rows), ZERO)),
                },
            )
        )

    def _report_stock_summary(self, request, client_id, *args):
        from apps.inventory.views import StockPositionView

        return StockPositionView().get(request)

    def _report_stock_movement(self, request, client_id, date_from, date_to, as_of):
        from apps.inventory.models import StockMovement
        from apps.inventory.serializers import StockMovementSerializer

        queryset = StockMovement.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).select_related("item", "location").order_by("-movement_date")
        item_id = request.query_params.get("itemId")
        if item_id:
            queryset = queryset.filter(item_id=item_id)
        if date_from:
            queryset = queryset.filter(movement_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(movement_date__lte=date_to)

        return Response(
            envelope(StockMovementSerializer(queryset[:2000], many=True).data)
        )

    def _report_hrms_attendance(self, request, client_id, date_from, date_to, as_of):
        from datetime import datetime

        from apps.hrms import services as hrms_services

        month = request.query_params.get("month")
        parsed = None
        if month:
            try:
                parsed = datetime.strptime(month[:7], "%Y-%m").date()
            except ValueError:
                raise ValidationFailed(
                    "Month must be YYYY-MM.", field_errors={"month": ["Expected YYYY-MM."]}
                )
        return Response(
            envelope(
                hrms_services.attendance_summary(
                    client_id,
                    month=parsed,
                    department_id=request.query_params.get("departmentId"),
                )
            )
        )

    def _report_hrms_payroll_summary(self, request, client_id, date_from, date_to, as_of):
        from datetime import datetime

        from apps.hrms import services as hrms_services

        month = request.query_params.get("month")
        parsed = None
        if month:
            try:
                parsed = datetime.strptime(month[:7], "%Y-%m").date()
            except ValueError:
                raise ValidationFailed(
                    "Month must be YYYY-MM.", field_errors={"month": ["Expected YYYY-MM."]}
                )
        return Response(hrms_services.payroll_summary(client_id, parsed))

    def _report_hrms_attrition(self, request, client_id, date_from, date_to, as_of):
        from apps.hrms.models import Employee

        employees = Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)
        exits = employees.filter(status__in=["Resigned", "Terminated"])
        if date_from:
            exits = exits.filter(last_working_day__gte=date_from)
        if date_to:
            exits = exits.filter(last_working_day__lte=date_to)

        headcount = employees.filter(
            status__in=["Active", "On Leave", "Probation"]
        ).count()
        by_month = (
            exits.annotate(month=TruncMonth("last_working_day"))
            .values("month")
            .annotate(count=Count("id"))
            .order_by("month")
        )
        by_department = (
            exits.values("department__name").annotate(count=Count("id")).order_by("-count")
        )
        return Response(
            {
                "headcount": headcount,
                "exits": exits.count(),
                "attritionRatePct": round(exits.count() / headcount * 100, 1)
                if headcount
                else 0,
                "byMonth": [
                    {"month": row["month"], "count": row["count"]} for row in by_month
                ],
                "byDepartment": [
                    {
                        "department": row["department__name"] or "Unassigned",
                        "count": row["count"],
                    }
                    for row in by_department
                ],
            }
        )


class ReportExportView(APIView):
    """``POST /reports/{reportKey}/export/`` -> ``{ downloadUrl }`` (api.md §12)."""

    permission_classes = [HasModulePermission]
    required_permissions = ["export_excel"]

    def post(self, request, report_key):
        import csv
        import io

        from apps.core.files import build_storage_key, public_url, write_bytes
        from apps.core.models import ExportJob, File

        fmt = (request.data.get("format") or "csv").lower()
        if fmt not in ("csv", "xlsx", "pdf"):
            raise ValidationFailed(
                "Unsupported export format.",
                field_errors={"format": ["Expected csv, xlsx or pdf."]},
            )
        if fmt != "csv":
            # Only CSV is generated here; xlsx/pdf need a renderer, and
            # returning a mislabelled CSV would be worse than saying so.
            raise ValidationFailed(
                f"{fmt.upper()} export is not configured on this server.",
                code="EXPORT_FORMAT_UNAVAILABLE",
                detail="CSV export is available.",
            )

        job = ExportJob.objects.create(
            client_id=request.client_id,
            report_key=report_key,
            format=fmt,
            params=request.data.get("params") or {},
            status="running",
            created_by=request.user,
        )

        try:
            # Reuse the read endpoint so an export can never drift from the
            # report it claims to be.
            request._request.GET = request._request.GET.copy()
            for key, value in (request.data.get("params") or {}).items():
                request._request.GET[key] = str(value)
            response = ReportView().get(request, report_key)
            payload = response.data
            rows = payload.get("results") if isinstance(payload, dict) else None
            if rows is None:
                rows = [payload] if isinstance(payload, dict) else list(payload or [])

            buffer = io.StringIO()
            if rows:
                writer = csv.DictWriter(
                    buffer, fieldnames=list(rows[0].keys()), extrasaction="ignore"
                )
                writer.writeheader()
                for row in rows:
                    writer.writerow(
                        {k: ("" if v is None else v) for k, v in row.items()}
                    )
            data = buffer.getvalue().encode("utf-8")

            file_row = File.objects.create(
                client_id=request.client_id,
                scope="export",
                storage_key=build_storage_key(
                    request.client_id, "export", f"{report_key}.csv"
                ),
                file_name=f"{report_key}-{timezone.localdate()}.csv",
                content_type="text/csv",
                file_size=len(data),
                status="committed",
                uploaded_by=request.user,
                committed_at=timezone.now(),
            )
            write_bytes(file_row.storage_key, data)

            job.file = file_row
            job.status = "done"
            job.completed_at = timezone.now()
            job.save(update_fields=["file", "status", "completed_at", "updated_at"])

            return Response(
                {
                    "jobId": str(job.id),
                    "status": job.status,
                    "downloadUrl": public_url(file_row, request),
                    "rowCount": len(rows),
                }
            )
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            job.save(update_fields=["status", "error", "updated_at"])
            raise


class MainDashboardView(APIView):
    """``GET /dashboard/`` -- the main screen payload (api.md §12)."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_financial_reports"]

    def get(self, request):
        from apps.accounting.services import account_balances
        from apps.crm.models import Lead
        from apps.masters.models import Item
        from apps.purchase.models import PurchaseBill, PurchaseOrder
        from apps.sales.models import SalesInvoice, SalesOrder

        client_id = request.client_id
        today = timezone.localdate()
        month_start = today.replace(day=1)

        invoices = SalesInvoice.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).exclude(status__in=["Cancelled", "Draft"])
        bills = PurchaseBill.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).exclude(status__in=["Cancelled", "Draft"])

        sales_month = invoices.filter(doc_date__gte=month_start).aggregate(
            value=money_sum("total"), count=Count("id")
        )
        purchase_month = bills.filter(doc_date__gte=month_start).aggregate(
            value=money_sum("total"), count=Count("id")
        )
        receivable = invoices.aggregate(
            total=money_sum("total"), paid=money_sum("amount_paid")
        )
        payable = bills.aggregate(total=money_sum("total"), paid=money_sum("amount_paid"))

        items = list(
            Item.objects.filter(client_id=client_id, deleted_at__isnull=True)
            .exclude(item_kind="Service")
            .only("id", "sku", "name", "reorder_level", "cost_price")
        )
        stock.annotate_items_with_stock(client_id, items)
        alerts = [
            {
                "itemId": str(item.id),
                "sku": item.sku,
                "name": item.name,
                "available": round4(item.available_qty),
                "reorderLevel": round4(item.reorder_level),
                "status": item.stock_status,
            }
            for item in items
            if item.stock_status in ("Low Stock", "Critical")
        ][:20]

        recent_invoices = invoices.order_by("-doc_date")[:5]
        recent_orders = SalesOrder.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        ).order_by("-doc_date")[:5]

        monthly = (
            invoices.filter(doc_date__gte=today - timedelta(days=365))
            .annotate(month=TruncMonth("doc_date"))
            .values("month")
            .annotate(value=money_sum("total"))
            .order_by("month")
        )

        return Response(
            {
                "sales": {
                    "monthToDate": round2(sales_month["value"]),
                    "invoiceCount": sales_month["count"],
                    "openOrders": SalesOrder.objects.filter(
                        client_id=client_id, deleted_at__isnull=True
                    ).exclude(stage__in=["Invoiced", "Cancelled"]).count(),
                    "monthly": [
                        {"month": row["month"], "value": round2(row["value"])}
                        for row in monthly
                    ],
                },
                "purchase": {
                    "monthToDate": round2(purchase_month["value"]),
                    "billCount": purchase_month["count"],
                    "openOrders": PurchaseOrder.objects.filter(
                        client_id=client_id, deleted_at__isnull=True
                    ).exclude(status__in=["Received", "Cancelled"]).count(),
                    "awaitingReceipt": bills.filter(goods_received=False).count(),
                },
                "cash": {
                    "receivable": round2(receivable["total"] - receivable["paid"]),
                    "payable": round2(payable["total"] - payable["paid"]),
                    "bankAccounts": account_balances(client_id),
                },
                "inventoryAlerts": alerts,
                "crm": {
                    "openLeads": Lead.objects.filter(
                        client_id=client_id, deleted_at__isnull=True
                    ).exclude(stage__is_won=True).exclude(stage__is_lost=True).count(),
                },
                "recentDocuments": [
                    {
                        "type": "SalesInvoice",
                        "id": str(row.id),
                        "number": row.invoice_number,
                        "party": row.party_name,
                        "date": row.doc_date,
                        "total": round2(row.total),
                        "status": row.status,
                    }
                    for row in recent_invoices
                ]
                + [
                    {
                        "type": "SalesOrder",
                        "id": str(row.id),
                        "number": row.order_number,
                        "party": row.party_name,
                        "date": row.doc_date,
                        "total": round2(row.total),
                        "status": row.stage,
                    }
                    for row in recent_orders
                ],
            }
        )
