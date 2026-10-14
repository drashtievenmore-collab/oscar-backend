"""
Print payloads (api.md §12.1).

Every printable document returns the same envelope -- company profile, the
document, and the computed totals in words -- so the in-app preview and the
customer-facing PDF render from one source (api-integration.md §10.5).

PDF rendering itself is a separate concern; ``/pdf/`` returns 501 with a clear
code rather than a broken file, so the frontend can fall back to the print
payload until a renderer is wired up.
"""
from django.utils import timezone

from .exceptions import EvenmoreAPIError
from .files import public_url
from .money import amount_in_words, round2


class PdfNotAvailable(EvenmoreAPIError):
    status_code = 501
    default_code = "PDF_RENDERER_UNAVAILABLE"
    default_message = "PDF rendering is not configured on this server."


class EmailNotConfigured(EvenmoreAPIError):
    status_code = 503
    default_code = "EMAIL_NOT_CONFIGURED"
    default_message = (
        "Outbound email is not configured on this server. "
        "Set EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, "
        "EMAIL_HOST_PASSWORD and DEFAULT_FROM_EMAIL."
    )


class EmailDeliveryFailed(EvenmoreAPIError):
    status_code = 502
    default_code = "EMAIL_DELIVERY_FAILED"
    default_message = "The email could not be delivered."


def company_payload(client_id, request=None):
    from .models import CompanyProfile

    profile = CompanyProfile.objects.filter(client_id=client_id).select_related(
        "logo_file", "signature_file", "bank_account"
    ).first()
    if profile is None:
        return {}

    return {
        "legalName": profile.legal_name,
        "tradeName": profile.trade_name,
        "gstin": profile.gstin,
        "pan": profile.pan,
        "cin": profile.cin,
        "address": profile.address,
        "state": profile.state,
        "phone": profile.phone,
        "email": profile.email,
        "website": profile.website,
        "logo": public_url(profile.logo_file, request),
        "signature": public_url(profile.signature_file, request),
        "bank": (
            {
                "name": profile.bank_account.name,
                "accountNumber": profile.bank_account.account_number,
                "ifsc": profile.bank_account.ifsc,
                "bankName": profile.bank_account.bank_name,
                "branch": profile.bank_account.branch,
            }
            if profile.bank_account_id
            else None
        ),
    }


def print_payload(document, serializer_class, *, request, title, terms_key=None):
    """The JSON print payload of api.md §12.1."""
    from .models import Setting

    client_id = document.client_id
    templates = Setting.objects.filter(
        client_id=client_id, key="print_templates"
    ).values_list("value", flat=True).first() or {}

    currency = "INR"
    try:
        currency = document.client.currency
    except Exception:  # pragma: no cover - client not loaded
        pass

    return {
        "title": title,
        "company": company_payload(client_id, request),
        "document": serializer_class(document, context={"request": request}).data,
        "totals": {
            "grandTotal": round2(getattr(document, "total", 0)),
            "amountInWords": amount_in_words(getattr(document, "total", 0), currency),
            "taxableValue": round2(getattr(document, "taxable_value", 0)),
            "totalTax": round2(getattr(document, "total_tax", 0)),
            "cgst": round2(getattr(document, "cgst", 0)),
            "sgst": round2(getattr(document, "sgst", 0)),
            "igst": round2(getattr(document, "igst", 0)),
        },
        "terms": (
            getattr(document, "terms", None)
            or (templates.get(terms_key) if terms_key else None)
            or templates.get("defaultTerms")
        ),
        "generatedAt": timezone.now(),
    }


def send_payload(document, *, channel, recipients, subject=None, message=None, actor=None,
                 attachments=None):
    """``POST /{module}/{entity}/{id}/send/`` (api.md §12.1).

    ``channel="email"`` is delivered for real through Django's SMTP backend
    and then audited. Anything else (e.g. ``whatsapp``) only records the
    intent, because delivery needs a provider that is not wired up — the
    response says so honestly via ``"sent": False``.

    ``attachments`` is a list of ``(filename, content_bytes, mimetype)``
    tuples carried on the email (e.g. the quotation PDF).
    """
    from .audit import record_audit

    sent = False
    delivery_note = None
    if (channel or "").lower() == "email":
        sent = _deliver_email(
            recipients=recipients or [],
            subject=subject or f"{document} — document",
            message=message or "",
            attachments=attachments or [],
        )
    else:
        delivery_note = (
            f"No delivery provider is wired up for channel {channel!r}; "
            "the intent was recorded but nothing was sent."
        )

    record_audit(
        client=document.client_id,
        actor=actor,
        action="send",
        entity_type=document.__class__.__name__,
        entity_id=document.id,
        entity_label=str(document),
        description=f"Queued for {channel} to {', '.join(recipients or []) or 'no recipients'}",
        after={"channel": channel, "recipients": recipients, "subject": subject, "sent": sent},
    )
    payload = {
        "queued": True,
        "sent": sent,
        "channel": channel,
        "recipients": recipients or [],
        "subject": subject,
        "message": message,
    }
    if delivery_note:
        payload["note"] = delivery_note
    return payload


def _quote_action_urls(message):
    """Extract the first ``/quote/...`` share URL and derive decision links.

    The public accept/reject endpoints are POST-only (link prefetchers must
    never trigger a decision), so the email buttons link back to the
    customer-facing quote page — ``?decision=accept`` / ``?decision=reject``
    only preselects the section; the customer still confirms with one click.
    Returns ``(view_url, accept_url, reject_url)`` or ``(None, None, None)``.
    """
    import re

    if not message:
        return None, None, None
    match = re.search(r"https?://[^\s'\"]*?/quote/[^\s'\"]+", message)
    if not match:
        return None, None, None
    view_url = match.group(0).rstrip(".,;)")
    sep = "&" if "?" in view_url else "?"
    return view_url, f"{view_url}{sep}decision=accept", f"{view_url}{sep}decision=reject"


def _html_email_body(message):
    """Plain-text body -> HTML with clickable links + decision buttons."""
    import html
    import re

    safe = html.escape(message or "")
    # Linkify bare URLs.
    safe = re.sub(
        r"(https?://[^\s<]+)",
        lambda m: f'<a href="{html.escape(m.group(1).rstrip(".,;)"), quote=True)}">{html.escape(m.group(1))}</a>',
        safe,
    )
    body = safe.replace("\n", "<br>")
    view_url, accept_url, reject_url = _quote_action_urls(message or "")
    if not view_url:
        return f"<div>{body}</div>"
    buttons = (
        '<div style="margin:24px 0 8px 0;">'
        f'<a href="{html.escape(view_url, quote=True)}" '
        'style="display:inline-block;padding:12px 24px;margin:4px;background:#2563eb;'
        'color:#ffffff;text-decoration:none;border-radius:8px;font-weight:bold;">'
        "View Quotation</a><br>"
        f'<a href="{html.escape(accept_url, quote=True)}" '
        'style="display:inline-block;padding:12px 24px;margin:4px;background:#16a34a;'
        'color:#ffffff;text-decoration:none;border-radius:8px;font-weight:bold;">'
        "Accept Quotation</a> "
        f'<a href="{html.escape(reject_url, quote=True)}" '
        'style="display:inline-block;padding:12px 24px;margin:4px;background:#ffffff;'
        'color:#dc2626;text-decoration:none;border-radius:8px;font-weight:bold;'
        'border:1px solid #fca5a5;">'
        "Reject</a>"
        "</div>"
        '<p style="color:#64748b;font-size:12px;">'
        "Accept / Reject opens your secure quotation page — "
        "confirm your choice there and the supplier is notified immediately."
        "</p>"
    )
    return f"<div>{body}</div>{buttons}"


def _deliver_email(*, recipients, subject, message, attachments=()):
    """Send a text+HTML email to every recipient. Raises on failure."""
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives

    to = [r for r in (recipients or []) if r]
    if not to:
        raise EmailDeliveryFailed("Add at least one recipient email address.")
    if not getattr(settings, "EMAIL_HOST", ""):
        raise EmailNotConfigured()
    sender = getattr(settings, "DEFAULT_FROM_EMAIL", "") or getattr(
        settings, "EMAIL_HOST_USER", ""
    )
    try:
        mail = EmailMultiAlternatives(
            subject=subject,
            body=message,
            from_email=sender or None,
            to=to,
        )
        mail.attach_alternative(_html_email_body(message), "text/html")
        for filename, content, mimetype in attachments or ():
            mail.attach(filename, content, mimetype)
        mail.send(fail_silently=False)
    except (EmailNotConfigured, EmailDeliveryFailed):
        raise
    except Exception as exc:
        raise EmailDeliveryFailed(f"SMTP delivery failed: {exc}")
    return True
