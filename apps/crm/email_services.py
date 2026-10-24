"""
Outbound lead email delivery service.

Integrates Django's email backend with CRM Lead emails, ensuring:
- Strict validation of recipient email addresses, subject, and body.
- Real delivery through configured SMTP / email backend.
- Accurate status recording ('Sent' vs 'Failed') without false positives.
- Audit logging with credential sanitization.
"""
import html
import re
from email.utils import make_msgid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.utils import timezone
from apps.core.audit import record_audit
from apps.core.exceptions import BusinessRuleViolation, ValidationFailed
from .models import LeadEmail


def _sanitize_error(exc):
    msg = str(exc)
    pwd = getattr(settings, "EMAIL_HOST_PASSWORD", "")
    if pwd and pwd in msg:
        msg = msg.replace(pwd, "******")
    return msg


def _html_body(body_text):
    if not body_text:
        return "<div></div>"
    if re.search(r"<[a-z][\s\S]*>", body_text, re.IGNORECASE):
        return f'<div style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #1e293b;">{body_text}</div>'
    safe = html.escape(body_text)
    safe = re.sub(
        r"(https?://[^\s<]+)",
        lambda m: f'<a href="{html.escape(m.group(1).rstrip(".,;)"), quote=True)}" target="_blank" rel="noopener">{html.escape(m.group(1))}</a>',
        safe,
    )
    safe_body = safe.replace("\n", "<br>")
    return f'<div style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #1e293b;">{safe_body}</div>'


def _plain_body(body_text):
    if not body_text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", body_text)
    return html.unescape(clean).strip()


def send_lead_email(lead, *, to_addresses, subject, body, actor=None, client_id=None):
    """
    Sends an outbound lead email through the configured Django email backend,
    persists a LeadEmail record with actual delivery status, logs audit trails,
    and returns the created record or raises a clear exception.
    """
    # 1. Recipient validation
    if isinstance(to_addresses, str):
        to_addresses = [a.strip() for a in to_addresses.split(",") if a.strip()]
    elif not isinstance(to_addresses, list):
        to_addresses = list(to_addresses) if to_addresses else []

    if not to_addresses:
        raise ValidationFailed({"to_addresses": ["Enter at least one valid recipient email address."]})

    clean_to = []
    for addr in to_addresses:
        addr_str = str(addr or "").strip()
        if not addr_str:
            continue
        try:
            validate_email(addr_str)
            clean_to.append(addr_str)
        except ValidationError:
            raise ValidationFailed({"to_addresses": [f"'{addr_str}' is not a valid email address."]})

    if not clean_to:
        raise ValidationFailed({"to_addresses": ["Enter at least one valid recipient email address."]})

    # 2. Subject & Body validation
    subject = (subject or "").strip()
    if not subject:
        raise ValidationFailed({"subject": ["Email subject is required."]})

    body = (body or "").strip()
    if not body:
        raise ValidationFailed({"body": ["Email message / description is required."]})

    client_id = client_id or getattr(lead, "client_id", None)

    # 3. Check email provider configuration
    is_locmem = getattr(settings, "EMAIL_BACKEND", "").endswith("locmem.EmailBackend")
    is_console = getattr(settings, "EMAIL_BACKEND", "").endswith("console.EmailBackend")
    has_host = bool(getattr(settings, "EMAIL_HOST", ""))
    if not (is_locmem or is_console or has_host or getattr(settings, "DEBUG", False)):
        raise BusinessRuleViolation(
            "Email sending is not configured on the server. Please configure EMAIL_HOST and SMTP credentials in the environment.",
            code="EMAIL_NOT_CONFIGURED",
        )

    # 4. Sender address
    sender = getattr(settings, "DEFAULT_FROM_EMAIL", "") or getattr(settings, "EMAIL_HOST_USER", "")
    if not sender:
        sender = "noreply@evenmore.erp"

    msg_id = make_msgid(domain="evenmore.erp")
    reply_to = []
    if actor and getattr(actor, "email", None):
        reply_to = [actor.email]

    plain_text = _plain_body(body)
    html_content = _html_body(body)

    # 5. Attempt provider delivery
    try:
        mail = EmailMultiAlternatives(
            subject=subject,
            body=plain_text,
            from_email=sender,
            to=clean_to,
            reply_to=reply_to or None,
            headers={"Message-ID": msg_id},
        )
        mail.attach_alternative(html_content, "text/html")
        mail.send(fail_silently=False)

        # 6. Save as Sent
        email_record = LeadEmail.objects.create(
            client_id=client_id,
            lead=lead,
            subject=subject,
            body=body,
            to_addresses=clean_to,
            sent_at=timezone.now(),
            provider_message_id=msg_id,
            status="Sent",
            error_message=None,
            created_by=actor if getattr(actor, "is_authenticated", False) else None,
        )

        record_audit(
            client=client_id,
            actor=actor,
            action="send_email",
            entity_type="CrmLead",
            entity_id=lead.id,
            entity_label=str(lead),
            description=f"Sent email '{subject}' to {', '.join(clean_to)}",
            after={"emailId": str(email_record.id), "status": "Sent", "recipients": clean_to},
        )

        return email_record

    except (ValidationFailed, BusinessRuleViolation):
        raise
    except Exception as exc:
        clean_error = _sanitize_error(exc)

        # 7. Record Failed attempt without marking sent_at
        failed_record = LeadEmail.objects.create(
            client_id=client_id,
            lead=lead,
            subject=subject,
            body=body,
            to_addresses=clean_to,
            sent_at=None,
            provider_message_id=None,
            status="Failed",
            error_message=clean_error,
            created_by=actor if getattr(actor, "is_authenticated", False) else None,
        )

        record_audit(
            client=client_id,
            actor=actor,
            action="email_failed",
            entity_type="CrmLead",
            entity_id=lead.id,
            entity_label=str(lead),
            description=f"Failed to send email '{subject}' to {', '.join(clean_to)}: {clean_error}",
            after={"emailId": str(failed_record.id), "status": "Failed", "error": clean_error},
        )

        raise BusinessRuleViolation(
            f"Email delivery failed: {clean_error}",
            code="EMAIL_DELIVERY_FAILED",
        )
