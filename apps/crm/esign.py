"""DocuSign e-signature for CRM contracts.

``POST /crm/contracts/{id}/send-for-signature/`` renders the contract PDF,
creates a DocuSign envelope addressed to the customer, and stores the
envelope id on the row. ``GET /crm/contracts/{id}/signature-status/`` polls
the envelope; once it is ``completed`` the signed PDF is downloaded into
``core.File`` and ``signed_file`` / ``signed_at`` are set.

Polling (rather than DocuSign Connect webhooks) keeps this working on a dev
machine that DocuSign cannot reach.

Auth is JWT Grant (server-to-server, no user login). The access token is
cached until shortly before it expires. Plain ``urllib`` -- no extra HTTP
dependency; PyJWT + cryptography sign the RS256 assertion.
"""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

from apps.core.exceptions import EvenmoreAPIError

#: Anchor strings drawn (in white) on the PDF; DocuSign places tabs on them.
SIGN_ANCHOR = "/sn1/"
DATE_ANCHOR = "/ds1/"

_TOKEN_CACHE_KEY = "docusign_access_token"
_CONSENT_REDIRECT = "https://developers.docusign.com/platform/auth/consent"


class ESignNotConfigured(EvenmoreAPIError):
    status_code = 503
    default_code = "ESIGN_NOT_CONFIGURED"
    default_message = (
        "E-signature is not configured on this server. Set "
        "DOCUSIGN_INTEGRATION_KEY, DOCUSIGN_USER_ID, DOCUSIGN_ACCOUNT_ID and "
        "DOCUSIGN_PRIVATE_KEY_PATH."
    )


class ESignConsentRequired(EvenmoreAPIError):
    status_code = 503
    default_code = "ESIGN_CONSENT_REQUIRED"
    default_message = "DocuSign needs a one-time consent before sending."


class ESignFailed(EvenmoreAPIError):
    status_code = 502
    default_code = "ESIGN_FAILED"
    default_message = "DocuSign rejected the request."


def is_configured():
    return all(
        (
            settings.DOCUSIGN_INTEGRATION_KEY,
            settings.DOCUSIGN_USER_ID,
            settings.DOCUSIGN_ACCOUNT_ID,
            settings.DOCUSIGN_PRIVATE_KEY_PATH,
        )
    )


def consent_url():
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "scope": "signature impersonation",
            "client_id": settings.DOCUSIGN_INTEGRATION_KEY,
            "redirect_uri": _CONSENT_REDIRECT,
        }
    )
    return f"https://{settings.DOCUSIGN_AUTH_SERVER}/oauth/auth?{query}"


def _private_key():
    path = Path(settings.DOCUSIGN_PRIVATE_KEY_PATH)
    if not path.is_absolute():
        path = Path(settings.BASE_DIR) / path
    try:
        return path.read_text()
    except OSError:
        raise ESignNotConfigured(
            f"DocuSign private key not found at {path}."
        )


def _request(method, url, *, data=None, headers=None, raw=False):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as handle:
            body = handle.read()
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {"message": text[:300]}
        return exc.code, payload
    except urllib.error.URLError as exc:
        raise ESignFailed(f"Could not reach DocuSign: {exc.reason}")
    return 200, body if raw else json.loads(body.decode() or "{}")


def access_token():
    if not is_configured():
        raise ESignNotConfigured()
    cached = cache.get(_TOKEN_CACHE_KEY)
    if cached:
        return cached

    import jwt

    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": settings.DOCUSIGN_INTEGRATION_KEY,
            "sub": settings.DOCUSIGN_USER_ID,
            "aud": settings.DOCUSIGN_AUTH_SERVER,
            "iat": now,
            "exp": now + 3600,
            "scope": "signature impersonation",
        },
        _private_key(),
        algorithm="RS256",
    )
    status_code, payload = _request(
        "POST",
        f"https://{settings.DOCUSIGN_AUTH_SERVER}/oauth/token",
        data=urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }
        ).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if status_code != 200:
        if payload.get("error") == "consent_required":
            url = consent_url()
            raise ESignConsentRequired(
                f"DocuSign needs a one-time consent before sending. Open {url} "
                "while signed in as the DocuSign user, click Accept, then retry.",
                payload={"consentUrl": url},
            )
        raise ESignFailed(
            f"DocuSign login failed: {payload.get('error_description') or payload.get('error') or status_code}"
        )
    token = payload["access_token"]
    cache.set(_TOKEN_CACHE_KEY, token, max(int(payload.get("expires_in", 3600)) - 300, 60))
    return token


def _api(method, path, *, body=None, raw=False):
    url = f"{settings.DOCUSIGN_BASE_URL}/v2.1/accounts/{settings.DOCUSIGN_ACCOUNT_ID}{path}"
    headers = {"Authorization": f"Bearer {access_token()}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    status_code, payload = _request(method, url, data=data, headers=headers, raw=raw)
    if status_code == 401:
        cache.delete(_TOKEN_CACHE_KEY)
    if status_code >= 300:
        raise ESignFailed(
            f"DocuSign error: {payload.get('message') or payload.get('errorCode') or status_code}"
        )
    return payload


def create_envelope(*, pdf_bytes, file_name, subject, signer_email, signer_name, message=None):
    """Create and send an envelope. Returns DocuSign's ``{envelopeId, status, ...}``."""
    anchor = {"anchorUnits": "pixels", "anchorXOffset": "0", "anchorYOffset": "0",
              "anchorIgnoreIfNotPresent": "false"}
    return _api(
        "POST",
        "/envelopes",
        body={
            "emailSubject": subject[:100],
            "emailBlurb": (message or "")[:10000],
            "documents": [
                {
                    "documentBase64": base64.b64encode(pdf_bytes).decode(),
                    "name": file_name,
                    "fileExtension": "pdf",
                    "documentId": "1",
                }
            ],
            "recipients": {
                "signers": [
                    {
                        "email": signer_email,
                        "name": signer_name,
                        "recipientId": "1",
                        "routingOrder": "1",
                        "tabs": {
                            "signHereTabs": [{**anchor, "anchorString": SIGN_ANCHOR}],
                            "dateSignedTabs": [{**anchor, "anchorString": DATE_ANCHOR}],
                        },
                    }
                ]
            },
            "status": "sent",
        },
    )


def envelope_status(envelope_id):
    return _api("GET", f"/envelopes/{envelope_id}")


def download_signed(envelope_id):
    """The completed document(s) plus certificate, as one PDF."""
    return _api("GET", f"/envelopes/{envelope_id}/documents/combined", raw=True)


# ---------------------------------------------------------------------------
# Contract PDF
# ---------------------------------------------------------------------------
def build_contract_pdf(contract, company=None):
    """Render a contract as PDF bytes with DocuSign signature anchors."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    company = company or {}
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm, title=contract.contract_number,
    )
    styles = getSampleStyleSheet()
    normal = ParagraphStyle("body", parent=styles["Normal"], fontSize=9.5, leading=13)
    heading = ParagraphStyle("h", parent=styles["Heading3"], fontSize=11, spaceBefore=8)
    anchor = ParagraphStyle("anchor", parent=normal, textColor=colors.white, fontSize=6)

    def para(text, style=normal):
        return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)

    company_name = company.get("tradeName") or company.get("legalName") or ""
    party_name = contract.party.name if contract.party_id else ""
    value = f"Rs. {contract.value:,.2f}" if contract.value is not None else "-"

    story = []
    if company_name:
        story.append(para(company_name, styles["Heading2"]))
    story += [
        para(contract.title or "Contract", styles["Title"]),
        Spacer(1, 4 * mm),
    ]
    rows = [
        ["Contract Number", contract.contract_number],
        ["Contract Type", contract.contract_type or "-"],
        ["Customer", party_name or "-"],
        ["Start Date", contract.start_date.strftime("%d %b %Y")],
        ["End Date", contract.end_date.strftime("%d %b %Y")],
        ["Contract Value", value],
    ]
    table = Table([[para(k), para(v)] for k, v in rows], colWidths=[45 * mm, None])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(table)

    if contract.description:
        story += [para("Scope", heading), para(contract.description)]
    if contract.body:
        story.append(para("Terms & Conditions", heading))
        for line in contract.body.splitlines():
            if line.strip():
                story.append(para(line.strip()))

    signature = Table(
        [
            [para(f"For {company_name or 'the Company'}"), para(f"For {party_name or 'the Customer'}")],
            ["", Paragraph(SIGN_ANCHOR, anchor)],
            [para("Authorised Signatory"), para("Signature")],
            ["", Paragraph(DATE_ANCHOR, anchor)],
            ["", para("Date")],
        ],
        colWidths=[None, None],
        rowHeights=[None, 18 * mm, None, 8 * mm, None],
    )
    signature.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    story += [Spacer(1, 12 * mm), signature]

    doc.build(story)
    return buffer.getvalue()
