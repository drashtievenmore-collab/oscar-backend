"""Quotation PDF attachment for customer emails (api.md §5.3, §12.1).

The ``/pdf/`` route intentionally returns 501 until a renderer is wired up.
This module is that renderer for quotations: ``build_quotation_pdf`` turns a
Quotation row into a print-ready A4 PDF (reportlab, pure Python, no system
dependencies) so ``POST /sales/quotations/{id}/send/`` can attach it to the
outbound email instead of sending a link-only message.
"""
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .money import amount_in_words, round2


def _text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        parts = [
            value.get("line1") or value.get("line") or "",
            value.get("line2") or "",
            ", ".join(p for p in (value.get("city"), value.get("state"), value.get("pincode")) if p),
        ]
        return ", ".join(p for p in (parts[0], parts[1], parts[2]) if p)
    return str(value)


def _money(value):
    return f"Rs. {round2(value or 0):,.2f}"


def build_quotation_pdf(document, company=None):
    """Render one quotation as PDF bytes. Raises nothing PDF-specific.

    ``document`` is a sales Quotation with ``line_items``; ``company`` is the
    ``company_payload`` dict (may be empty when no profile exists).
    """
    company = company or {}
    lines = list(
        document.line_items.filter(deleted_at__isnull=True).order_by("line_no")
    )

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=str(document),
    )
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    title_style.fontSize = 18
    heading = styles["Heading3"]
    normal = styles["Normal"]
    normal.fontSize = 9
    normal.leading = 12
    small = styles["Normal"].__class__("small", parent=styles["Normal"])
    small.fontSize = 8
    small.leading = 10
    small.textColor = colors.HexColor("#475569")

    story = []

    company_name = (
        company.get("tradeName") or company.get("legalName") or "Quotation"
    )
    story.append(Paragraph(company_name, title_style))
    company_bits = [
        company.get("address") if isinstance(company.get("address"), str) else _text(company.get("address")),
        " ".join(
            p for p in (
                f"GSTIN: {company.get('gstin')}" if company.get("gstin") else "",
                f"Phone: {company.get('phone')}" if company.get("phone") else "",
                company.get("email") or "",
            )
            if p
        ),
    ]
    story.append(Paragraph("<br/>".join(b for b in company_bits if b), small))
    story.append(Spacer(1, 4 * mm))

    story.append(
        Paragraph(f"QUOTATION — {document.quotation_number or ''}", heading)
    )
    meta = [
        ["Customer", _text(document.party_name), "Date", str(document.doc_date or "—")],
        ["GSTIN", _text(document.party_gstin) or "—", "Valid Until", str(document.valid_until or "—")],
        ["Place of Supply", _text(document.place_of_supply) or "—", "Status", _text(document.status) or "—"],
    ]
    if document.subject:
        meta.append(["Subject", _text(document.subject), "", ""])
    meta_table = Table(meta, colWidths=[28 * mm, 62 * mm, 28 * mm, 52 * mm])
    meta_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#475569")),
                ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#475569")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(meta_table)
    story.append(Spacer(1, 4 * mm))

    header = ["#", "Item", "HSN", "Qty", "Rate", "Disc %", "Tax %", "Amount"]
    rows = [header]
    for line in lines:
        rows.append(
            [
                str(line.line_no),
                "<br/>".join(
                    p
                    for p in (
                        _text(line.item_name or line.description),
                        _text(line.description) if line.item_name else "",
                    )
                    if p
                ) or "—",
                _text(line.hsn_code) or "—",
                f"{line.qty} {_text(line.uom)}".strip(),
                _money(line.rate),
                str(line.discount_pct or 0),
                str(line.tax_pct or 0),
                _money(line.line_total),
            ]
        )
    # Platypus cells need Paragraphs for wrapping; plain strings are fine for
    # short values, item names become Paragraphs.
    body = []
    for i, row in enumerate(rows):
        if i == 0:
            body.append(row)
            continue
        cells = list(row)
        cells[1] = Paragraph(cells[1], normal)
        body.append(cells)

    items_table = Table(
        body,
        colWidths=[8 * mm, 62 * mm, 18 * mm, 22 * mm, 22 * mm, 14 * mm, 14 * mm, 24 * mm],
        repeatRows=1,
    )
    items_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(items_table)
    story.append(Spacer(1, 4 * mm))

    totals = [
        ["Subtotal", _money(document.subtotal)],
        ["Discount", _money(document.total_discount)],
        ["Tax", _money(document.total_tax)],
        ["Freight", _money(document.freight_charges)],
        ["Other Charges", _money(document.other_charges)],
        ["Round Off", _money(document.round_off)],
        ["Grand Total", _money(document.total)],
    ]
    totals_table = Table(totals, colWidths=[120 * mm, 50 * mm])
    totals_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEBELOW", (0, -2), (-1, -2), 0.5, colors.HexColor("#cbd5e1")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, -1), (-1, -1), 11),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(totals_table)
    story.append(Spacer(1, 2 * mm))

    try:
        currency = "INR"
        story.append(
            Paragraph(
                f"Amount in words: {amount_in_words(document.total, currency)}",
                small,
            )
        )
    except Exception:
        pass

    if document.terms:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph("Terms", heading))
        story.append(Paragraph(_text(document.terms).replace("\n", "<br/>"), normal))
    if document.notes:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph("Notes", heading))
        story.append(Paragraph(_text(document.notes).replace("\n", "<br/>"), normal))

    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph("Generated from Evenmore ERP", small))

    doc.build(story)
    buffer.seek(0)
    return buffer.read()
