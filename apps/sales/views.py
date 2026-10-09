"""Sales endpoints (api.md §5)."""
import secrets
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    NotFound,
    ValidationFailed,
)
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.printing import PdfNotAvailable, print_payload, send_payload
from apps.core.viewsets import TenantModelViewSet
from apps.inventory import services as stock

from . import services
from .models import (
    DeliveryChallan,
    DeliveryChallanLine,
    PaymentAllocation,
    PaymentIn,
    ProformaInvoice,
    ProformaInvoiceLine,
    Quotation,
    QuotationActivity,
    QuotationShare,
    SalesInvoice,
    SalesInvoiceLine,
    SalesOrder,
    SalesOrderLine,
    SalesReturn,
    SalesReturnLine,
    WarrantyCard,
)
from .serializers import (
    AllocateSerializer,
    ConvertLinesSerializer,
    DeliveryChallanSerializer,
    InvoiceOutstandingSerializer,
    PaymentInSerializer,
    ProformaInvoiceSerializer,
    QuotationActivitySerializer,
    QuotationSerializer,
    ReasonSerializer,
    SalesInvoiceSerializer,
    SalesOrderSerializer,
    SalesReturnSerializer,
    ShareRequestSerializer,
    WarrantyCardSerializer,
)

MONEY = DecimalField(max_digits=18, decimal_places=2)


def money_sum(field, **kwargs):
    return Coalesce(Sum(field, **kwargs), Value(Decimal("0.00")), output_field=MONEY)


class SalesDocumentViewSet(TenantModelViewSet):
    """Shared behaviour for the sales document family.

    Drafts are editable and deletable; anything posted is cancelled instead
    (api.md §1.9). Every document gets ``/print/``, ``/pdf/`` and ``/send/``
    (api.md §12.1).
    """

    draft_only_writes = True
    idempotent_create = True
    default_date_field = "doc_date"
    allowed_date_fields = ("doc_date", "created_at")
    search_fields = ["party_name", "notes"]
    ordering = ["-doc_date", "-created_at"]
    print_title = "Document"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("party")
            .prefetch_related("line_items__item")
        )

    def get_aggregates(self, queryset):
        rows = queryset.aggregate(
            count=Count("id"),
            totalValue=money_sum("total"),
            paid=money_sum("amount_paid"),
        )
        rows["outstanding"] = round2(rows["totalValue"] - rows["paid"])
        return rows

    @action(detail=True, methods=["get"])
    def print(self, request, pk=None):
        document = self.get_object()
        return Response(
            print_payload(
                document,
                self.get_serializer_class(),
                request=request,
                title=self.print_title,
            )
        )

    @action(detail=True, methods=["get"])
    def pdf(self, request, pk=None):
        self.get_object()
        raise PdfNotAvailable(
            detail="Use /print/ for the JSON payload and render client-side for now."
        )

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        from apps.crm import services as crm_services

        document = self.get_object()
        channel = request.data.get("channel", "email")
        attachments = []
        if (channel or "").lower() == "email":
            attachments = self._send_attachments(document, request)
        payload = send_payload(
            document,
            channel=channel,
            recipients=request.data.get("recipients") or [],
            subject=request.data.get("subject"),
            message=request.data.get("message"),
            actor=request.user,
            attachments=attachments,
        )
        # A sent quotation advances its deal out of Draft.
        if (
            document.__class__.__name__ == "Quotation"
            and (channel or "").lower() == "email"
            and payload.get("sent")
        ):
            crm_services.advance_deal_for_quotation(
                document, "Sent", actor=request.user,
                description=(
                    f"Quotation {document.quotation_number} sent to customer."
                ),
            )
        return Response(payload)

    def _send_attachments(self, document, request):
        """The quotation PDF rides on the customer email (api.md §5.3).

        A PDF that fails to render must not block the send — the link-only
        email still goes out, and the failure is logged for the backend
        console instead of surfacing as a 500 to the sender.
        """
        import logging

        from apps.core.printing import company_payload
        from apps.core.quotation_pdf import build_quotation_pdf

        logger = logging.getLogger(__name__)
        if document.__class__.__name__ != "Quotation":
            return []
        try:
            pdf = build_quotation_pdf(
                document, company_payload(document.client_id, request)
            )
        except Exception:
            logger.exception(
                "Quotation PDF render failed for %s; sending link-only email",
                document,
            )
            return []
        filename = f"{document.quotation_number or 'quotation'}.pdf"
        return [(filename, pdf, "application/pdf")]


def _clone_document(source, target_model, overrides, *, number_field, series,
                    line_model_name, line_fk, line_filter=None, line_overrides=None):
    """Copy a document and its lines into the next stage of the pipeline.

    Every frozen field is carried across rather than re-read from the party, so
    a conversion preserves what the customer actually agreed to (db.md §3.2).
    """
    import importlib

    from apps.core.money import round2

    copied_fields = [
        "client_id", "party_id", "party_name", "party_gstin", "billing_address",
        "shipping_address", "place_of_supply", "notes", "terms",
        "freight_charges", "other_charges", "discount_override",
    ]
    payload = {field: getattr(source, field) for field in copied_fields}
    payload["doc_date"] = timezone.localdate()
    payload.update(overrides)

    target = target_model(**payload)
    setattr(target, number_field, allocate_number(source.client, series, target.doc_date))
    target.save()

    module = importlib.import_module(target_model.__module__)
    line_model = getattr(module, line_model_name)

    source_lines = source.line_items.filter(deleted_at__isnull=True).order_by("line_no")
    if line_filter is not None:
        source_lines = [line for line in source_lines if line_filter(line)]

    line_fields = [
        "client_id", "line_no", "item_id", "sku", "item_name", "description",
        "hsn_code", "uom", "qty", "rate", "discount_pct", "tax_pct",
        "is_bom_generated", "bom_source_item_id", "is_user_modified", "parent_sku",
    ]
    for line in source_lines:
        data = {field: getattr(line, field) for field in line_fields}
        data[line_fk] = target
        if line_overrides:
            data.update(line_overrides(line))
        line_model.objects.create(**data)

    services.recalculate_document(target)
    return target


# ---------------------------------------------------------------------------
# Quotations (api.md §5.3)
# ---------------------------------------------------------------------------
class QuotationViewSet(SalesDocumentViewSet):
    queryset = Quotation.objects.all()
    serializer_class = QuotationSerializer
    audit_entity_type = "Quotation"
    audit_label_field = "quotation_number"
    status_field = "status"
    print_title = "Quotation"
    filter_map = {"customerId": "party_id", "customer_id": "party_id"}
    permission_map = {"read": ["view_sales"], "write": ["create_quotation"]}
    draft_values = ("Draft", "Sent")

    def perform_create(self, serializer):
        serializer.validated_data["quotation_number"] = allocate_number(
            self.request.user.client, "QT", serializer.validated_data.get("doc_date")
        )
        return super().perform_create(serializer)

    @action(detail=True, methods=["get"])
    def pdf(self, request, pk=None):
        """Download the rendered quotation PDF (api.md §5.3, §12.1).

        The base viewset answers 501 for documents with no renderer; the
        quotation renderer lives in ``apps/core/quotation_pdf.py``, so this
        endpoint streams the same file the customer email attaches — the
        sender downloads it here and attaches it to the WhatsApp chat
        manually, since ``wa.me`` links can only prefill text.
        """
        import logging
        from io import BytesIO

        from django.http import FileResponse

        from apps.core.printing import PdfNotAvailable, company_payload
        from apps.core.quotation_pdf import build_quotation_pdf

        document = self.get_object()
        try:
            pdf = build_quotation_pdf(
                document, company_payload(document.client_id, request)
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Quotation PDF render failed for %s", document
            )
            raise PdfNotAvailable(detail="The quotation PDF could not be rendered.")
        filename = f"{document.quotation_number or 'quotation'}.pdf"
        return FileResponse(
            BytesIO(pdf),
            content_type="application/pdf",
            filename=filename,
        )

    @action(detail=True, methods=["post"], url_path="convert-to-order")
    @transaction.atomic
    def convert_to_order(self, request, pk=None):
        quotation = self.get_object()
        if quotation.status in ("Converted", "Cancelled"):
            raise Conflict(
                f"This quotation is {quotation.status}.", code=Codes.ALREADY_DONE
            )

        order = _clone_document(
            quotation,
            SalesOrder,
            {"quotation": quotation, "stage": "Draft"},
            number_field="order_number",
            series="SO",
            line_model_name="SalesOrderLine",
            line_fk="sales_order",
        )
        quotation.status = "Converted"
        quotation.save(update_fields=["status", "updated_at"])
        self.write_audit(
            "convert", quotation, description=f"Converted to {order.order_number}"
        )
        return Response(
            SalesOrderSerializer(order, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="convert-to-challan")
    @transaction.atomic
    def convert_to_challan(self, request, pk=None):
        """The direct-to-challan path (api.md §5.3)."""
        quotation = self.get_object()
        challan = _clone_document(
            quotation,
            DeliveryChallan,
            {"quotation": quotation, "status": "Draft"},
            number_field="challan_number",
            series="DC",
            line_model_name="DeliveryChallanLine",
            line_fk="delivery_challan",
        )
        self.write_audit(
            "convert", quotation, description=f"Challan {challan.challan_number} created"
        )
        return Response(
            DeliveryChallanSerializer(challan, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="approve")
    @transaction.atomic
    def approve(self, request, pk=None):
        """Customer approval: Accepted + automatic Lead -> Customer conversion.

        Transactional: if customer creation/linking fails, the approval rolls
        back too. Idempotent: re-approving an Accepted quotation links (never
        duplicates) and returns the same customer. Never creates a Sales Order.
        """
        from apps.crm import services as crm_services

        quotation = Quotation.objects.select_for_update().get(
            pk=self.get_object().pk
        )
        if quotation.status in ("Rejected", "Cancelled", "Expired"):
            raise Conflict(
                f"This quotation is {quotation.status} and cannot be approved.",
                code=Codes.ALREADY_DONE,
            )

        conversion = {"party": None, "created": False, "lead": None}
        if quotation.status not in ("Accepted", "Converted", "Invoiced"):
            quotation.status = "Accepted"
            quotation.save(update_fields=["status", "updated_at"])
            QuotationActivity.objects.create(
                quotation=quotation,
                event="accepted",
                actor_label=getattr(request.user, "name", None) or "staff",
                comment=request.data.get("reason"),
            )
            self.write_audit(
                "approve", quotation,
                description=f"Approved ({quotation.quotation_number})",
            )

        if quotation.crm_lead_id is not None:
            conversion = crm_services.convert_lead_to_customer(
                quotation.crm_lead,
                user=request.user,
                source="Quotation Approval",
                reference=quotation.quotation_number,
            )
            quotation.crm_lead = conversion["lead"]

        party = conversion["party"]
        # The approved quotation stays linked to the customer: point it at the
        # converted (or matched) party so the follow-up Sales Order is raised
        # on the right customer. The frozen address snapshot is only filled
        # where the quotation has none, preserving what was agreed.
        if party is not None and quotation.party_id != party.id:
            quotation.party = party
            quotation.freeze_party_snapshot(party)
            quotation.save()
        # An approval wins the linked deal: same transaction, so a failed
        # deal move rolls the approval back too.
        crm_services.advance_deal_for_quotation(
            quotation, "Won", actor=request.user,
            description=(
                f"Deal won by approval of quotation "
                f"{quotation.quotation_number}."
            ),
        )
        quotation.refresh_from_db()

        lead_payload = None
        if conversion["lead"] is not None:
            from apps.crm.serializers import LeadSerializer

            lead_payload = LeadSerializer(conversion["lead"]).data

        return Response(
            {
                "quotation": QuotationSerializer(
                    quotation, context=self.get_serializer_context()
                ).data,
                "customer": (
                    {"id": str(party.id), "name": party.name, "code": party.code}
                    if party is not None
                    else None
                ),
                "customerCreated": conversion["created"],
                "lead": lead_payload,
                "message": (
                    "Quotation approved. Lead has been converted to Customer "
                    "successfully." if party is not None
                    else "Quotation approved."
                ),
            }
        )

    @action(detail=True, methods=["post"], url_path="accept")
    @transaction.atomic
    def accept(self, request, pk=None):
        """Alias of approve: staff-side acceptance (Approved/Accepted)."""
        return self.approve(request, pk=pk)

    @action(detail=True, methods=["post", "delete"], url_path="share")
    def share(self, request, pk=None):
        """Opaque, hashed, single-quotation, expiring, revocable (api.md §5.3).

        Replaces ``localQuotationSharing.js``, whose base64 token was a
        security placeholder.
        """
        quotation = self.get_object()

        if request.method == "DELETE":
            revoked = QuotationShare.objects.filter(
                quotation=quotation, revoked_at__isnull=True
            ).update(revoked_at=timezone.now(), revoked_by=request.user)
            QuotationActivity.objects.create(
                quotation=quotation, event="revoked", actor_label=request.user.name
            )
            return Response({"revoked": revoked})

        serializer = ShareRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        token = secrets.token_urlsafe(32)
        share = QuotationShare.objects.create(
            client_id=request.client_id,
            quotation=quotation,
            token_hash=_hash_token(token),
            recipients=data["recipients"],
            channel=data["channel"],
            expires_at=timezone.now() + timedelta(days=data["expiryDays"]),
            created_by=request.user,
        )
        QuotationActivity.objects.create(
            quotation=quotation, share=share, event="sent", actor_label=request.user.name
        )
        if quotation.status == "Draft":
            quotation.status = "Sent"
            quotation.save(update_fields=["status", "updated_at"])
        # Sharing is sending: the linked deal leaves Draft with the link.
        from apps.crm import services as crm_services

        crm_services.advance_deal_for_quotation(
            quotation, "Sent", actor=request.user,
            description=(
                f"Quotation {quotation.quotation_number} shared with customer."
            ),
        )

        # The token is returned once and never stored in the clear.
        return Response(
            {
                "token": token,
                "url": f"/quote/{quotation.quotation_number}/{token}",
                "expiresAt": share.expires_at,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"])
    def activity(self, request, pk=None):
        quotation = self.get_object()
        rows = quotation.activity.all()
        return Response(envelope(QuotationActivitySerializer(rows, many=True).data))


def _hash_token(token):
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Sales orders (api.md §5.4)
# ---------------------------------------------------------------------------
class SalesOrderViewSet(SalesDocumentViewSet):
    queryset = SalesOrder.objects.all()
    serializer_class = SalesOrderSerializer
    audit_entity_type = "SalesOrder"
    audit_label_field = "order_number"
    status_field = "stage"
    print_title = "Sales Order"
    filter_map = {
        "customerId": "party_id",
        "stage": "stage",
        "paymentStatus": "payment_status",
    }
    permission_map = {"read": ["view_sales"], "write": ["create_sales_order"]}
    draft_values = ("Draft",)

    def perform_create(self, serializer):
        serializer.validated_data["order_number"] = allocate_number(
            self.request.user.client, "SO", serializer.validated_data.get("doc_date")
        )
        return super().perform_create(serializer)

    def perform_update(self, serializer):
        """Confirming an order reserves stock (api.md §5.4).

        Reservation is not a column -- it is derived from open order lines
        (db.md §5.2) -- so "reserving" here means enforcing the credit limit
        and letting the stage change make the reservation true.
        """
        previous_stage = serializer.instance.stage
        order = super().perform_update(serializer)

        if previous_stage == "Draft" and order.stage == "Confirmed":
            services.assert_credit_limit(
                order.party,
                order.total,
                user=self.request.user,
                override=bool(self.request.data.get("overrideCreditLimit")),
            )
        return order

    def check_draft_only(self, instance, verb):
        # An order stays editable while it is Confirmed but nothing has shipped.
        if instance.stage in ("Draft", "Confirmed") and not instance.line_items.filter(
            dispatched_qty__gt=0
        ).exists():
            return
        super().check_draft_only(instance, verb)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def cancel(self, request, pk=None):
        """``{ reason }`` -- releases reservations (api.md §5.4)."""
        order = self.get_object()
        if order.stage == "Cancelled":
            raise Conflict("This order is already cancelled.", code=Codes.ALREADY_CANCELLED)

        services.assert_no_dependents(
            order,
            [
                ("Challan", order.challans.filter(deleted_at__isnull=True).exclude(
                    status="Cancelled")),
                ("Invoice", order.invoices.filter(deleted_at__isnull=True).exclude(
                    status="Cancelled")),
            ],
        )

        reason = request.data.get("reason")
        order.stage = "Cancelled"
        order.cancelled_at = timezone.now()
        order.cancelled_by = request.user
        order.cancellation_reason = reason
        order.save()
        # Moving to Cancelled takes the lines out of the reservation query,
        # which is what "releases the reservation" means here.
        self.write_audit("cancel", order, description=reason)
        return Response(self.get_serializer(order).data)

    @action(detail=True, methods=["get"])
    def fulfilment(self, request, pk=None):
        return Response(services.order_fulfilment(self.get_object()))

    @action(detail=True, methods=["post"], url_path="convert-to-challan")
    @transaction.atomic
    def convert_to_challan(self, request, pk=None):
        order = self.get_object()
        serializer = ConvertLinesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._convert(order, serializer.validated_data, target="challan")

    @action(detail=True, methods=["post"], url_path="convert-to-invoice")
    @transaction.atomic
    def convert_to_invoice(self, request, pk=None):
        order = self.get_object()
        serializer = ConvertLinesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._convert(order, serializer.validated_data, target="invoice")

    def _convert(self, order, data, *, target):
        """Partial conversion (api.md §5.4): ``{ lines: [{ lineId, qty, serials[] }] }``."""
        if order.stage == "Cancelled":
            raise Conflict("This order is cancelled.", code=Codes.ALREADY_CANCELLED)

        requested = {
            str(row.get("lineId") or row.get("line_id")): row for row in data.get("lines") or []
        }
        source_lines = list(order.line_items.filter(deleted_at__isnull=True).order_by("line_no"))

        selected = []
        for line in source_lines:
            row = requested.get(str(line.id))
            if requested and row is None:
                continue
            already = line.dispatched_qty if target == "challan" else line.invoiced_qty
            remaining = D(line.qty) - D(already)
            qty = D(row.get("qty")) if row and row.get("qty") is not None else remaining
            if qty <= ZERO:
                continue
            if qty > remaining + Decimal("0.0001"):
                code = Codes.OVER_DISPATCH if target == "challan" else Codes.OVER_INVOICE
                raise BusinessRuleViolation(
                    f"{line.item_name}: only {round4(remaining)} left to "
                    f"{'dispatch' if target == 'challan' else 'invoice'}.",
                    code=code,
                    payload={"lineId": str(line.id), "remaining": str(round4(remaining))},
                )
            selected.append((line, qty, (row or {}).get("serials") or []))

        if not selected:
            raise ValidationFailed(
                "Nothing left to convert on this order.",
                field_errors={"lines": ["Every line is already fully converted."]},
            )

        if target == "challan":
            document = DeliveryChallan(
                client_id=order.client_id,
                party=order.party,
                party_name=order.party_name,
                party_gstin=order.party_gstin,
                billing_address=order.billing_address,
                shipping_address=order.shipping_address,
                place_of_supply=order.place_of_supply,
                doc_date=data.get("date") or timezone.localdate(),
                sales_order=order,
                status="Draft",
                location_id=data.get("locationId"),
                notes=data.get("notes"),
                created_by=self.request.user,
            )
            document.challan_number = allocate_number(
                order.client, "DC", document.doc_date
            )
        else:
            document = SalesInvoice(
                client_id=order.client_id,
                party=order.party,
                party_name=order.party_name,
                party_gstin=order.party_gstin,
                billing_address=order.billing_address,
                shipping_address=order.shipping_address,
                place_of_supply=order.place_of_supply,
                doc_date=data.get("date") or timezone.localdate(),
                sales_order=order,
                status="Draft",
                location_id=data.get("locationId"),
                notes=data.get("notes"),
                created_by=self.request.user,
            )
        document.save()

        line_model = DeliveryChallanLine if target == "challan" else SalesInvoiceLine
        line_fk = "delivery_challan" if target == "challan" else "sales_invoice"
        line_table = (
            "delivery_challan_lines" if target == "challan" else "sales_invoice_lines"
        )

        for index, (source, qty, serials) in enumerate(selected, start=1):
            new_line = line_model.objects.create(
                client_id=order.client_id,
                **{line_fk: document},
                line_no=index,
                item_id=source.item_id,
                sku=source.sku,
                item_name=source.item_name,
                description=source.description,
                hsn_code=source.hsn_code,
                uom=source.uom,
                qty=qty,
                rate=source.rate,
                discount_pct=source.discount_pct,
                tax_pct=source.tax_pct,
                sales_order_line=source,
                is_bom_generated=source.is_bom_generated,
                bom_source_item_id=source.bom_source_item_id,
                parent_sku=source.parent_sku,
            )
            if serials:
                resolved = stock.resolve_serials(order.client_id, source.item_id, serials)
                stock.link_line_serials(order.client_id, line_table, new_line.id, resolved)

        services.recalculate_document(document)

        serializer_class = (
            DeliveryChallanSerializer if target == "challan" else SalesInvoiceSerializer
        )
        self.write_audit(
            "convert",
            order,
            description=f"Converted to {target}: {services.number_of(document)}",
        )
        return Response(
            serializer_class(document, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="create-project")
    @transaction.atomic
    def create_project(self, request, pk=None):
        """Hand off to PMS (api.md §5.4, §10.2)."""
        from apps.pms.serializers import ProjectDetailSerializer
        from apps.pms.views import create_project_from_order

        order = self.get_object()
        if order.pms_project_id:
            raise Conflict(
                f"This order already has project {order.pms_project.code}.",
                code=Codes.ALREADY_DONE,
                payload={"projectId": str(order.pms_project_id)},
            )
        project = create_project_from_order(
            order,
            project_manager_id=request.data.get("projectManagerId"),
            priority=request.data.get("priority", "Medium"),
            stage_config_ids=request.data.get("stageConfigIds") or [],
            user=request.user,
        )
        return Response(
            ProjectDetailSerializer(project, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Proforma invoices (api.md §5.5)
# ---------------------------------------------------------------------------
class ProformaInvoiceViewSet(SalesDocumentViewSet):
    queryset = ProformaInvoice.objects.all()
    serializer_class = ProformaInvoiceSerializer
    audit_entity_type = "ProformaInvoice"
    audit_label_field = "proforma_number"
    status_field = "status"
    print_title = "Proforma Invoice"
    filter_map = {"customerId": "party_id"}
    permission_map = {"read": ["view_sales"], "write": ["view_sales"]}
    draft_values = ("Draft", "Sent")

    def perform_create(self, serializer):
        serializer.validated_data["proforma_number"] = allocate_number(
            self.request.user.client, "PI", serializer.validated_data.get("doc_date")
        )
        return super().perform_create(serializer)

    @action(detail=True, methods=["post"], url_path="convert-to-invoice")
    @transaction.atomic
    def convert_to_invoice(self, request, pk=None):
        """A proforma moves no stock and posts no ledger entry (api.md §5.5)."""
        proforma = self.get_object()
        if proforma.status == "Converted":
            raise Conflict("This proforma is already converted.", code=Codes.ALREADY_DONE)

        invoice = _clone_document(
            proforma,
            SalesInvoice,
            {
                "proforma_invoice": proforma,
                "sales_order": proforma.sales_order,
                "status": "Draft",
            },
            number_field="invoice_number",
            series="INV",
            line_model_name="SalesInvoiceLine",
            line_fk="sales_invoice",
        )
        # The number is allocated for real at finalization; a draft keeps none.
        invoice.invoice_number = None
        invoice.save(update_fields=["invoice_number"])

        proforma.status = "Converted"
        proforma.save(update_fields=["status", "updated_at"])
        return Response(
            SalesInvoiceSerializer(invoice, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Delivery challans (api.md §5.6)
# ---------------------------------------------------------------------------
class DeliveryChallanViewSet(SalesDocumentViewSet):
    queryset = DeliveryChallan.objects.all()
    serializer_class = DeliveryChallanSerializer
    audit_entity_type = "DeliveryChallan"
    audit_label_field = "challan_number"
    status_field = "status"
    print_title = "Delivery Challan"
    filter_map = {"customerId": "party_id", "salesOrderId": "sales_order_id"}
    permission_map = {"read": ["view_sales"], "write": ["create_sales_order"]}

    def perform_create(self, serializer):
        serializer.validated_data["challan_number"] = allocate_number(
            self.request.user.client, "DC", serializer.validated_data.get("doc_date")
        )
        return super().perform_create(serializer)

    def perform_update(self, serializer):
        """A status change to ``Dispatched`` posts the stock movements."""
        previous = serializer.instance.status
        challan = super().perform_update(serializer)
        if previous == "Draft" and challan.status == "Dispatched":
            services.dispatch_challan(challan, user=self.request.user)
            challan.refresh_from_db()
        return challan

    # ``dispatch`` is APIView's own entry point: a method of that name here
    # shadows it and breaks every request to this viewset. Keep the URL,
    # rename the method.
    @action(detail=True, methods=["post"], url_path="dispatch", url_name="dispatch")
    def dispatch_action(self, request, pk=None):
        challan = services.dispatch_challan(self.get_object(), user=request.user)
        self.write_audit("dispatch", challan, description="Challan dispatched")
        return Response(self.get_serializer(challan).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        challan = services.cancel_challan(
            self.get_object(),
            reason=serializer.validated_data.get("reason"),
            user=request.user,
        )
        self.write_audit("cancel", challan, description=serializer.validated_data.get("reason"))
        return Response(self.get_serializer(challan).data)

    @action(detail=True, methods=["post"], url_path="convert-to-invoice")
    @transaction.atomic
    def convert_to_invoice(self, request, pk=None):
        """Bill a dispatched challan (api.md §5.6).

        Invoice lines carry ``delivery_challan_line``, which is how
        finalization knows not to deplete the stock a second time
        (api.md §5.7 rule 3).
        """
        challan = self.get_object()
        if challan.status == "Cancelled":
            raise Conflict("This challan is cancelled.", code=Codes.ALREADY_CANCELLED)

        invoice = SalesInvoice.objects.create(
            client_id=challan.client_id,
            party=challan.party,
            party_name=challan.party_name,
            party_gstin=challan.party_gstin,
            billing_address=challan.billing_address,
            shipping_address=challan.shipping_address,
            place_of_supply=challan.place_of_supply,
            doc_date=timezone.localdate(),
            sales_order=challan.sales_order,
            delivery_challan=challan,
            location=challan.location,
            status="Draft",
            freight_charges=challan.freight_charges,
            other_charges=challan.other_charges,
            created_by=request.user,
        )
        for index, line in enumerate(
            challan.line_items.filter(deleted_at__isnull=True).order_by("line_no"), start=1
        ):
            SalesInvoiceLine.objects.create(
                client_id=challan.client_id,
                sales_invoice=invoice,
                line_no=index,
                item_id=line.item_id,
                sku=line.sku,
                item_name=line.item_name,
                description=line.description,
                hsn_code=line.hsn_code,
                uom=line.uom,
                qty=line.qty,
                rate=line.rate,
                discount_pct=line.discount_pct,
                tax_pct=line.tax_pct,
                sales_order_line=line.sales_order_line,
                delivery_challan_line=line,
                is_bom_generated=line.is_bom_generated,
                bom_source_item_id=line.bom_source_item_id,
                parent_sku=line.parent_sku,
            )
        services.recalculate_document(invoice)
        return Response(
            SalesInvoiceSerializer(invoice, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="warranty-card")
    @transaction.atomic
    def warranty_card(self, request, pk=None):
        """Issue a warranty card for the dispatched serials (api.md §5.6)."""
        from .models import WarrantyCardItem, WarrantyCardSerial

        challan = self.get_object()
        if challan.status == "Draft":
            raise Conflict(
                "Dispatch the challan before issuing a warranty card.",
                code=Codes.NOT_FINALIZED,
            )

        period = int(request.data.get("warrantyPeriod") or 1)
        unit = request.data.get("warrantyUnit") or "Years"
        start_event = request.data.get("warrantyStartEvent") or "Delivery"
        start_date = challan.dispatch_date or challan.doc_date

        card = WarrantyCard.objects.create(
            client_id=challan.client_id,
            card_number=allocate_number(challan.client, "WC"),
            party=challan.party,
            contact_person=request.data.get("contactPerson"),
            billing_address=challan.billing_address,
            shipping_address=challan.shipping_address,
            gstin=challan.party_gstin,
            delivery_challan=challan,
            sales_order=challan.sales_order,
            delivery_date=start_date,
            delivery_location=challan.delivery_location,
            warranty_period=period,
            warranty_unit=unit,
            warranty_start_event=start_event,
            start_date=start_date,
            expiry_date=services.compute_expiry(start_date, period, unit),
            document_status="Generated",
            created_by=request.user,
        )

        line_ids = list(
            challan.line_items.filter(deleted_at__isnull=True).values_list("id", flat=True)
        )
        serial_map = stock.serials_for_lines(
            challan.client_id, "delivery_challan_lines", line_ids
        )
        for line in challan.line_items.filter(deleted_at__isnull=True):
            WarrantyCardItem.objects.create(
                client_id=challan.client_id,
                warranty_card=card,
                item_id=line.item_id,
                sku=line.sku,
                item_name=line.item_name,
                qty=line.qty,
            )
            numbers = serial_map.get(line.id, [])
            if numbers:
                resolved = stock.resolve_serials(
                    challan.client_id, line.item_id, numbers, expected_status=None
                )
                WarrantyCardSerial.objects.bulk_create(
                    [
                        WarrantyCardSerial(
                            client_id=challan.client_id, warranty_card=card, serial=serial
                        )
                        for serial in resolved
                    ],
                    ignore_conflicts=True,
                )
                stock.set_serial_status(resolved, "sold", warranty_card=card)

        return Response(
            WarrantyCardSerializer(card, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Sales invoices (api.md §5.7)
# ---------------------------------------------------------------------------
class SalesInvoiceViewSet(SalesDocumentViewSet):
    queryset = SalesInvoice.objects.all()
    serializer_class = SalesInvoiceSerializer
    audit_entity_type = "SalesInvoice"
    audit_label_field = "invoice_number"
    status_field = "status"
    print_title = "Tax Invoice"
    filter_map = {"customerId": "party_id", "salesOrderId": "sales_order_id"}
    permission_map = {
        "read": ["view_sales"],
        "write": ["create_invoice"],
        "finalize": ["finalize_invoice"],
        "cancel": ["cancel_invoice"],
    }

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if self.request.query_params.get("overdue") == "true":
            queryset = queryset.filter(
                due_date__lt=timezone.localdate(),
                total__gt=F("amount_paid"),
            ).exclude(status__in=["Cancelled", "Draft", "Paid"])
        return queryset

    def get_aggregates(self, queryset):
        rows = queryset.aggregate(
            count=Count("id"),
            totalValue=money_sum("total"),
            paid=money_sum("amount_paid"),
            draft=Count("id", filter=Q(status="Draft")),
            overdue=Count(
                "id",
                filter=Q(due_date__lt=timezone.localdate(), total__gt=F("amount_paid"))
                & ~Q(status__in=["Cancelled", "Draft", "Paid"]),
            ),
        )
        rows["outstanding"] = round2(rows["totalValue"] - rows["paid"])
        return rows

    def perform_create(self, serializer):
        """``POST /sales/invoices/`` -- Draft unless ``{ finalize: true }``."""
        invoice = super().perform_create(serializer)
        if self.request.data.get("finalize"):
            services.finalize_invoice(
                invoice,
                user=self.request.user,
                override_credit_limit=bool(self.request.data.get("overrideCreditLimit")),
            )
            invoice.refresh_from_db()
        return invoice

    @action(detail=True, methods=["post"])
    def finalize(self, request, pk=None):
        invoice = services.finalize_invoice(
            self.get_object(),
            user=request.user,
            override_credit_limit=bool(request.data.get("overrideCreditLimit")),
        )
        self.write_audit(
            "finalize", invoice, description=f"Finalized as {invoice.invoice_number}"
        )
        return Response(self.get_serializer(invoice).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = services.cancel_invoice(
            self.get_object(),
            reason=serializer.validated_data.get("reason"),
            user=request.user,
        )
        self.write_audit("cancel", invoice, description=serializer.validated_data.get("reason"))
        return Response(self.get_serializer(invoice).data)

    @action(detail=True, methods=["get"])
    def outstanding(self, request, pk=None):
        return Response(services.invoice_outstanding(self.get_object()))

    @action(detail=True, methods=["post"], url_path="e-invoice")
    def e_invoice(self, request, pk=None):
        """IRN / e-way bill hook (api.md §5.7, phase 2)."""
        self.get_object()
        raise PdfNotAvailable(
            "E-invoicing is not configured on this workspace.",
            code="EINVOICE_NOT_CONFIGURED",
        )


# ---------------------------------------------------------------------------
# Payments in (api.md §5.8)
# ---------------------------------------------------------------------------
class PaymentInViewSet(TenantModelViewSet):
    queryset = PaymentIn.objects.select_related("party", "bank_account")
    serializer_class = PaymentInSerializer
    audit_entity_type = "PaymentIn"
    audit_label_field = "payment_number"
    idempotent_create = True
    status_field = "status"
    default_date_field = "payment_date"
    search_fields = ["payment_number", "reference_number", "party__name"]
    ordering = ["-payment_date", "-created_at"]
    filter_map = {"customerId": "party_id", "mode": "mode"}
    permission_map = {"read": ["view_sales"], "write": ["record_payment_in"]}

    def get_aggregates(self, queryset):
        rows = queryset.aggregate(
            count=Count("id"),
            totalReceived=money_sum("amount"),
            allocated=money_sum("allocated_amount"),
        )
        rows["unallocated"] = round2(rows["totalReceived"] - rows["allocated"])
        return rows

    @transaction.atomic
    def perform_create(self, serializer):
        data = serializer.validated_data
        allocations = self.request.data.get("allocations") or []
        invoice_id = self.request.data.get("invoiceId")
        invoice = None
        if invoice_id:
            invoice = SalesInvoice.objects.filter(
                pk=invoice_id, client_id=self.get_client_id(), deleted_at__isnull=True
            ).first()
            if invoice is None:
                raise NotFound("That invoice no longer exists.")

        payment = services.record_payment_in(
            client=self.request.user.client,
            party=data["party"],
            amount=data["amount"],
            payment_date=data["payment_date"],
            mode=data["mode"],
            bank_account=data.get("bank_account"),
            reference_number=data.get("reference_number"),
            notes=data.get("notes"),
            allocations=allocations,
            invoice=invoice,
            user=self.request.user,
        )
        serializer.instance = payment
        self._created_instance = payment
        self._concurrency_instance = payment
        self.write_audit("create", payment, description="Payment recorded")
        return payment

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = services.cancel_payment_in(
            self.get_object(),
            reason=serializer.validated_data.get("reason"),
            user=request.user,
        )
        self.write_audit("cancel", payment, description=serializer.validated_data.get("reason"))
        return Response(self.get_serializer(payment).data)

    @action(detail=True, methods=["post"])
    def allocate(self, request, pk=None):
        serializer = AllocateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = services.allocate_payment_in(
            self.get_object(), serializer.validated_data["allocations"], user=request.user
        )
        return Response(self.get_serializer(payment).data)

    @action(detail=False, methods=["get"])
    def unallocated(self, request):
        """``GET /sales/payments/unallocated/`` -- customer advances."""
        queryset = services.unallocated_payments(
            request.client_id, request.query_params.get("customerId")
        )
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)


# ---------------------------------------------------------------------------
# Sales returns (api.md §5.9)
# ---------------------------------------------------------------------------
class SalesReturnViewSet(SalesDocumentViewSet):
    queryset = SalesReturn.objects.select_related("sales_invoice")
    serializer_class = SalesReturnSerializer
    audit_entity_type = "SalesReturn"
    audit_label_field = "return_number"
    status_field = "status"
    print_title = "Credit Note"
    draft_only_writes = False
    filter_map = {"customerId": "party_id", "salesInvoiceId": "sales_invoice_id"}
    permission_map = {"read": ["view_sales"], "write": ["process_returns"]}

    @transaction.atomic
    def perform_create(self, serializer):
        """Enforces both return invariants of api.md §5.9 server-side."""
        invoice = serializer.validated_data["sales_invoice"]
        if invoice.status == "Cancelled":
            raise BusinessRuleViolation(
                "Cannot return against a cancelled invoice.",
                code=Codes.PAYMENT_ON_CANCELLED,
            )

        serializer.validated_data["return_number"] = allocate_number(
            self.request.user.client, "SR", serializer.validated_data.get("doc_date")
        )
        serializer.validated_data["credit_note_number"] = serializer.validated_data[
            "return_number"
        ]
        serializer.validated_data.setdefault("party", invoice.party)

        sales_return = super().perform_create(serializer)
        self._validate_and_post_return(sales_return, invoice)
        return sales_return

    def _validate_and_post_return(self, sales_return, invoice):
        from apps.accounting import services as ledger

        already_returned = stock.previously_returned_serials(
            sales_return.client_id, invoice.id
        )
        location_id = sales_return.location_id or invoice.location_id

        for line in sales_return.line_items.select_related(
            "sales_invoice_line", "item"
        ).all():
            invoice_line = line.sales_invoice_line
            remaining = D(invoice_line.qty) - D(invoice_line.returned_qty)
            if D(line.returned_qty) > remaining + Decimal("0.0001"):
                raise BusinessRuleViolation(
                    f"{line.item_name}: only {round4(remaining)} of "
                    f"{round4(invoice_line.qty)} remains returnable.",
                    code=Codes.OVER_RETURN,
                    payload={
                        "invoiceLineId": str(invoice_line.id),
                        "remaining": str(round4(remaining)),
                    },
                )

            serial_numbers = stock.serials_for_lines(
                sales_return.client_id, "sales_return_lines", [line.id]
            ).get(line.id, [])
            duplicates = [s for s in serial_numbers if s in already_returned]
            if duplicates:
                raise BusinessRuleViolation(
                    f"Serial{'s' if len(duplicates) > 1 else ''} already returned: "
                    f"{', '.join(duplicates)}.",
                    code=Codes.SERIAL_ALREADY_RETURNED,
                    payload={"serials": duplicates},
                )

            invoice_line.returned_qty = round4(
                D(invoice_line.returned_qty) + D(line.returned_qty)
            )
            invoice_line.save(update_fields=["returned_qty", "updated_at"])

            if line.item_id and line.item.holds_stock and location_id:
                movement = stock.post_movement(
                    client_id=sales_return.client_id,
                    item=line.item_id,
                    location=location_id,
                    type="SALES_RETURN",
                    quantity=D(line.returned_qty),
                    unit_cost=line.item.cost_price,
                    reference_type="SalesReturn",
                    reference_id=sales_return.id,
                    reference_number=sales_return.return_number,
                    movement_date=sales_return.doc_date,
                    user=self.request.user,
                )
                if serial_numbers:
                    resolved = stock.resolve_serials(
                        sales_return.client_id, line.item_id, serial_numbers,
                        expected_status=None,
                    )
                    stock.set_serial_status(resolved, "returned", movement=movement)

        entry = ledger.post_sales_return(sales_return, user=self.request.user)
        if entry is not None:
            sales_return.journal_entry = entry
            sales_return.save(update_fields=["journal_entry", "updated_at"])
        services.refresh_invoice_payment_status(invoice)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def cancel(self, request, pk=None):
        """Posts ``RETURN_CANCELLATION`` movements (api.md §5.9)."""
        sales_return = self.get_object()
        if sales_return.status == "Cancelled":
            raise Conflict("This return is already cancelled.", code=Codes.ALREADY_CANCELLED)

        reason = request.data.get("reason")
        services._reverse_document(sales_return, "SalesReturn", user=request.user, reason=reason)

        for line in sales_return.line_items.select_related("sales_invoice_line").all():
            invoice_line = line.sales_invoice_line
            invoice_line.returned_qty = max(
                round4(D(invoice_line.returned_qty) - D(line.returned_qty)), ZERO
            )
            invoice_line.save(update_fields=["returned_qty", "updated_at"])

        services.refresh_invoice_payment_status(sales_return.sales_invoice)
        self.write_audit("cancel", sales_return, description=reason)
        return Response(self.get_serializer(sales_return).data)


# ---------------------------------------------------------------------------
# Warranty cards (api.md §5.10)
# ---------------------------------------------------------------------------
class WarrantyCardViewSet(TenantModelViewSet):
    queryset = WarrantyCard.objects.select_related(
        "party", "delivery_challan", "sales_invoice"
    ).prefetch_related("items")
    serializer_class = WarrantyCardSerializer
    audit_entity_type = "WarrantyCard"
    audit_label_field = "card_number"
    status_field = "document_status"
    default_date_field = "start_date"
    search_fields = ["card_number", "party__name", "contact_person"]
    ordering = ["-created_at"]
    filter_map = {"customerId": "party_id"}
    permission_map = {"read": ["view_sales"], "write": ["view_sales"]}
    draft_only_writes = True
    draft_status_field = "document_status"
    draft_values = ("Draft",)

    def perform_create(self, serializer):
        data = serializer.validated_data
        data["card_number"] = allocate_number(self.request.user.client, "WC")
        if not data.get("expiry_date") and data.get("start_date"):
            data["expiry_date"] = services.compute_expiry(
                data["start_date"],
                data.get("warranty_period", 1),
                data.get("warranty_unit", "Years"),
            )
        return super().perform_create(serializer)

    def _transition(self, request, target, reason_field=None, allowed_from=None):
        card = self.get_object()
        if allowed_from and card.document_status not in allowed_from:
            raise Conflict(
                f"A {card.document_status} card cannot be {target.lower()}.",
                code=Codes.BAD_TARGET,
            )
        reason = request.data.get("reason")
        card.document_status = target
        if reason_field:
            setattr(card, reason_field, reason)
        card.save()
        self.write_audit(target.lower(), card, description=reason)
        return Response(self.get_serializer(card).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._transition(request, "Cancelled", "cancelled_reason")

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        return self._transition(request, "Void", "void_reason")

    @action(detail=True, methods=["post"])
    def suspend(self, request, pk=None):
        return self._transition(
            request, "Suspended", "suspended_reason", allowed_from=("Generated",)
        )

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        return self._transition(request, "Generated", allowed_from=("Suspended",))

    @action(detail=False, methods=["get"], url_path=r"by-challan/(?P<challan_id>[^/.]+)")
    def by_challan(self, request, challan_id=None):
        card = self.get_queryset().filter(delivery_challan_id=challan_id).first()
        if card is None:
            raise NotFound("No warranty card has been issued for that challan.")
        return Response(self.get_serializer(card).data)

    @action(detail=False, methods=["get"], url_path=r"by-serial/(?P<serial_no>[^/.]+)")
    def by_serial(self, request, serial_no=None):
        """Also used by service intake (api.md §5.10)."""
        card = (
            self.get_queryset()
            .filter(serial_links__serial__serial_no=serial_no)
            .distinct()
            .first()
        )
        if card is None:
            raise NotFound("No warranty card covers that serial number.")
        return Response(self.get_serializer(card).data)

    @action(detail=True, methods=["get"])
    def print(self, request, pk=None):
        card = self.get_object()
        return Response(
            print_payload(
                card, self.get_serializer_class(), request=request, title="Warranty Card"
            )
        )
