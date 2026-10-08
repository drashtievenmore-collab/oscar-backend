"""
Public, unauthenticated endpoints (api.md §5.3, §9.7, §10.6, §11.5).

These are the only routes outside the tenant-scoped API. Each resolves its own
tenant from the token or slug it was given -- never from a header the caller
controls -- and every one is rate-limited, because an opaque token is not a
rate limit.
"""
import hashlib
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import request_ip
from apps.core.exceptions import Codes, Conflict, NotFound, ValidationFailed
from apps.core.money import round2
from apps.core.permissions import AllowPublic
from apps.core.tenancy import set_current_client_id
from apps.core.throttling import PublicEndpointThrottle, PublicWriteThrottle


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


class PublicView(APIView):
    authentication_classes = []
    permission_classes = [AllowPublic]
    throttle_classes = [PublicEndpointThrottle]


class PublicWriteView(PublicView):
    throttle_classes = [PublicWriteThrottle]


# ---------------------------------------------------------------------------
# Quotation sharing (api.md §5.3)
# ---------------------------------------------------------------------------
def _resolve_quotation_share(quotation_number, token):
    from apps.sales.models import Quotation, QuotationShare

    share = (
        QuotationShare.objects.filter(token_hash=hash_token(token))
        .select_related("quotation", "quotation__party")
        .first()
    )
    if share is None:
        raise NotFound("This link is not valid.")

    quotation = share.quotation
    if quotation.quotation_number != quotation_number:
        raise NotFound("This link is not valid.")
    if share.revoked_at is not None:
        raise Conflict("This link has been revoked.", code=Codes.TOKEN_REVOKED)
    if share.expires_at < timezone.now():
        raise Conflict("This link has expired.", code=Codes.TOKEN_EXPIRED)

    set_current_client_id(quotation.client_id)
    return share, quotation


class PublicQuotationView(PublicView):
    """``GET /public/quotations/{number}/{token}/`` -- the customer-facing view."""

    def get(self, request, quotation_number, token):
        from apps.sales.models import QuotationActivity
        from apps.sales.serializers import QuotationSerializer

        share, quotation = _resolve_quotation_share(quotation_number, token)

        QuotationActivity.objects.create(
            quotation=quotation,
            share=share,
            event="viewed",
            actor_label="customer",
            ip=request_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
        )
        if quotation.status == "Sent":
            quotation.status = "Viewed"
            quotation.save(update_fields=["status", "updated_at"])

        from apps.core.printing import company_payload

        return Response(
            {
                "company": company_payload(quotation.client_id, request),
                "quotation": QuotationSerializer(
                    quotation, context={"request": request}
                ).data,
                "canDecide": quotation.status
                not in ("Accepted", "Rejected", "Converted", "Cancelled"),
                "expiresAt": share.expires_at,
            }
        )


class PublicQuotationDecisionView(PublicWriteView):
    """accept / reject / comment (api.md §5.3)."""

    decision = None

    def post(self, request, quotation_number, token):
        from apps.core.audit import notify
        from apps.sales.models import QuotationActivity

        share, quotation = _resolve_quotation_share(quotation_number, token)

        if self.decision == "comment":
            comment = (request.data.get("comment") or "").strip()
            if not comment:
                raise ValidationFailed(
                    "Enter a comment.", field_errors={"comment": ["Required."]}
                )
            QuotationActivity.objects.create(
                quotation=quotation, share=share, event="commented",
                actor_label=request.data.get("name") or "customer",
                comment=comment, ip=request_ip(request),
            )
            return Response({"recorded": True})

        if quotation.status in ("Accepted", "Rejected", "Converted", "Cancelled"):
            raise Conflict(
                f"This quotation has already been {quotation.status.lower()}.",
                code=Codes.ALREADY_DECIDED,
            )

        with transaction.atomic():
            quotation.status = "Accepted" if self.decision == "accept" else "Rejected"
            quotation.save(update_fields=["status", "updated_at"])
            QuotationActivity.objects.create(
                quotation=quotation,
                share=share,
                event="accepted" if self.decision == "accept" else "rejected",
                actor_label=request.data.get("name") or "customer",
                comment=request.data.get("reason"),
                ip=request_ip(request),
            )
            if self.decision == "accept" and quotation.crm_lead_id is not None:
                # Customer-portal approval converts the lead too, in the same
                # transaction: a failed conversion rolls back the approval.
                from apps.crm import services as crm_services

                crm_services.convert_lead_to_customer(
                    quotation.crm_lead,
                    user=None,
                    source="Customer Portal Approval",
                    reference=quotation.quotation_number,
                )
            if quotation.created_by_id:
                notify(
                    client=quotation.client_id,
                    recipients=[quotation.created_by_id],
                    type="sales.quotation_decided",
                    category="erp",
                    title=f"{quotation.quotation_number} was {quotation.status.lower()}",
                    body=f"{quotation.party_name} responded to the shared quotation.",
                    entity_type="Quotation",
                    entity_id=quotation.id,
                )

        return Response({"status": quotation.status})


class PublicQuotationAcceptView(PublicQuotationDecisionView):
    decision = "accept"


class PublicQuotationRejectView(PublicQuotationDecisionView):
    decision = "reject"


class PublicQuotationCommentView(PublicQuotationDecisionView):
    decision = "comment"


# ---------------------------------------------------------------------------
# PMS client proof approval (api.md §10.6)
# ---------------------------------------------------------------------------
def _resolve_proof_share(token):
    from apps.pms.models import ProofShare

    share = (
        ProofShare.objects.filter(token_hash=hash_token(token))
        .select_related("document", "document__file", "document__stage", "project")
        .first()
    )
    if share is None:
        raise NotFound("This approval link is not valid.")
    if share.status == "Revoked" or share.revoked_at is not None:
        raise Conflict("This approval link has been revoked.", code=Codes.TOKEN_REVOKED)
    if share.expires_at < timezone.now():
        if share.status != "Expired":
            share.status = "Expired"
            share.save(update_fields=["status", "updated_at"])
        raise Conflict("This approval link has expired.", code=Codes.TOKEN_EXPIRED)

    set_current_client_id(share.client_id)
    return share


class PublicProofView(PublicView):
    """``GET /public/pms/approve/{token}/`` -- marks ``openedAt`` on first fetch."""

    def get(self, request, token):
        from apps.core.files import public_url
        from apps.core.printing import company_payload

        share = _resolve_proof_share(token)
        if share.opened_at is None:
            share.opened_at = timezone.now()
            share.save(update_fields=["opened_at", "updated_at"])

        document = share.document
        project = share.project
        return Response(
            {
                "company": company_payload(share.client_id, request),
                "project": {
                    "id": str(project.id),
                    "code": project.code,
                    "customerName": project.customer_name,
                    "productName": project.product_name,
                },
                "stage": {
                    "id": str(document.stage_id),
                    "sequence": document.stage.sequence,
                    "name": document.stage.name,
                },
                "document": {
                    "id": str(document.id),
                    "fileName": document.file_name,
                    "fileSize": document.file_size,
                    "version": document.version,
                    "previewUrl": public_url(document.file, request),
                    "uploadedAt": document.uploaded_at,
                    "comments": document.comments,
                },
                "recipientName": share.recipient_name,
                "decision": share.decision,
                "decidedAt": share.decided_at,
                "canDecide": share.decision is None,
                "expiresAt": share.expires_at,
            }
        )


class PublicProofDecisionView(PublicWriteView):
    """``POST /public/pms/approve/{token}/decide/``.

    The decision writes straight through to the stage approval and therefore to
    the handoff gate, in one transaction (api.md §10.6).
    """

    def post(self, request, token):
        from apps.pms.views import apply_document_decision

        share = _resolve_proof_share(token)
        if share.decision is not None:
            raise Conflict(
                "A decision has already been recorded for this link.",
                code=Codes.ALREADY_DECIDED,
            )

        decision = request.data.get("decision")
        if decision not in ("Approved", "Need Improvement"):
            raise ValidationFailed(
                "Choose whether to approve or request changes.",
                field_errors={"decision": ["Expected 'Approved' or 'Need Improvement'."]},
            )

        decided_by = request.data.get("decidedBy") or share.recipient_name or "Client"
        comments = request.data.get("comments")
        revision_reason = request.data.get("revisionReason")

        with transaction.atomic():
            share.decision = decision
            share.decided_at = timezone.now()
            share.decided_by = decided_by
            share.decision_comments = comments
            share.revision_reason = revision_reason
            share.save()

            apply_document_decision(
                document=share.document,
                decision=decision,
                comments=comments,
                revision_reason=revision_reason,
                decided_by_label=decided_by,
                user=None,
            )

        return Response({"decision": decision, "decidedAt": share.decided_at})


# ---------------------------------------------------------------------------
# Public lead forms (api.md §9.7)
# ---------------------------------------------------------------------------
class PublicFormView(PublicView):
    """``GET /public/forms/{slug}/`` -- the form schema."""

    def get(self, request, slug):
        from apps.crm.models import Form

        form = Form.objects.filter(
            slug=slug, is_published=True, deleted_at__isnull=True
        ).first()
        if form is None:
            raise NotFound("This form is not available.")
        set_current_client_id(form.client_id)
        return Response({"id": str(form.id), "name": form.name, "schema": form.schema})


class PublicFormSubmitView(PublicWriteView):
    """``POST /public/forms/{slug}/submit/`` -- creates a lead."""

    def post(self, request, slug):
        from apps.core.numbering import allocate_number
        from apps.crm.models import Form, FormSubmission, Lead, Stage
        from apps.crm.services import run_stage_automation

        form = Form.objects.filter(
            slug=slug, is_published=True, deleted_at__isnull=True
        ).select_related("client").first()
        if form is None:
            raise NotFound("This form is not available.")
        set_current_client_id(form.client_id)

        payload = request.data if isinstance(request.data, dict) else {}
        name = (payload.get("name") or payload.get("fullName") or "").strip()
        if not name:
            raise ValidationFailed(
                "Please tell us your name.", field_errors={"name": ["Required."]}
            )

        stage = Stage.objects.filter(
            client_id=form.client_id, is_active=True, deleted_at__isnull=True
        ).order_by("sequence").first()
        if stage is None:
            raise NotFound("This form is not accepting submissions.")

        with transaction.atomic():
            lead = Lead.objects.create(
                client_id=form.client_id,
                lead_number=allocate_number(form.client, "LEAD"),
                name=name,
                company=payload.get("company"),
                phone=payload.get("phone"),
                email=payload.get("email"),
                city=payload.get("city"),
                state=payload.get("state"),
                country=payload.get("country"),
                job_title=payload.get("jobTitle"),
                stage=stage,
                custom_values=payload,
            )
            FormSubmission.objects.create(
                client_id=form.client_id,
                form=form,
                payload=payload,
                lead=lead,
                ip=request_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
            )
            run_stage_automation(lead, stage, user=None)

        # The public caller gets an acknowledgement, never the lead record.
        return Response(
            {"received": True, "reference": lead.lead_number},
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Public career portal (api.md §11.5)
# ---------------------------------------------------------------------------
class PublicCareersListView(PublicView):
    def get(self, request):
        from apps.hrms.models import Job

        jobs = Job.objects.filter(
            is_published=True, status="Open", deleted_at__isnull=True
        ).select_related("department", "location")
        return Response(
            {
                "results": [
                    {
                        "id": str(job.id),
                        "slug": job.slug,
                        "title": job.title,
                        "department": job.department.name if job.department_id else None,
                        "location": job.location.name if job.location_id else None,
                        "employmentType": job.employment_type,
                        "experienceRange": job.experience_range,
                        "publishedAt": job.published_at,
                    }
                    for job in jobs
                ]
            }
        )


class PublicCareerDetailView(PublicView):
    def get(self, request, job_id):
        from apps.hrms.models import Job, ScreeningQuestion

        job = Job.objects.filter(
            is_published=True, status="Open", deleted_at__isnull=True
        ).filter(slug=job_id).select_related("department", "location").first()
        if job is None:
            job = Job.objects.filter(
                pk=job_id, is_published=True, status="Open", deleted_at__isnull=True
            ).select_related("department", "location").first()
        if job is None:
            raise NotFound("This role is no longer open.")

        set_current_client_id(job.client_id)
        questions = ScreeningQuestion.objects.filter(
            client_id=job.client_id, deleted_at__isnull=True, is_active=True
        ).filter(models_q(job)).order_by("sort_order")

        return Response(
            {
                "id": str(job.id),
                "slug": job.slug,
                "title": job.title,
                "department": job.department.name if job.department_id else None,
                "location": job.location.name if job.location_id else None,
                "employmentType": job.employment_type,
                "experienceRange": job.experience_range,
                "salaryRange": job.salary_range,
                "description": job.description,
                "openings": job.openings,
                "questions": [
                    {
                        "id": str(question.id),
                        "question": question.question,
                        "type": question.type,
                        "options": question.options,
                    }
                    for question in questions
                ],
            }
        )


def models_q(job):
    """Global questions (``job_id is null``) plus this role's own."""
    from django.db.models import Q

    return Q(job__isnull=True) | Q(job=job)


class PublicCareerApplyView(PublicWriteView):
    """``POST /public/careers/{jobId}/apply/`` -- application + résumé."""

    def post(self, request, job_id):
        from apps.hrms.models import (
            Application,
            Candidate,
            Job,
            ScreeningAnswer,
            ScreeningQuestion,
        )

        job = Job.objects.filter(
            is_published=True, status="Open", deleted_at__isnull=True
        ).filter(slug=job_id).first()
        if job is None:
            job = Job.objects.filter(
                pk=job_id, is_published=True, status="Open", deleted_at__isnull=True
            ).first()
        if job is None:
            raise NotFound("This role is no longer open.")
        set_current_client_id(job.client_id)

        name = (request.data.get("name") or "").strip()
        email = (request.data.get("email") or "").strip()
        if not name or not email:
            raise ValidationFailed(
                "Name and email are required.",
                field_errors={
                    **({} if name else {"name": ["Required."]}),
                    **({} if email else {"email": ["Required."]}),
                },
            )

        with transaction.atomic():
            candidate = Candidate.objects.filter(
                client_id=job.client_id, email__iexact=email, deleted_at__isnull=True
            ).first()
            if candidate is None:
                candidate = Candidate.objects.create(
                    client_id=job.client_id,
                    name=name,
                    email=email,
                    phone=request.data.get("phone"),
                    resume_file_id=request.data.get("resumeFileId"),
                    source="careers_portal",
                    current_ctc=request.data.get("currentCtc") or None,
                    expected_ctc=request.data.get("expectedCtc") or None,
                    notice_period_days=request.data.get("noticePeriodDays") or None,
                    stage="Applied",
                )

            application, created = Application.objects.get_or_create(
                client_id=job.client_id,
                candidate=candidate,
                job=job,
                defaults={"stage": "Applied"},
            )
            if not created:
                raise Conflict(
                    "You have already applied for this role.",
                    code=Codes.ALREADY_DONE,
                )

            answers = request.data.get("answers") or {}
            if answers:
                questions = {
                    str(question.id): question
                    for question in ScreeningQuestion.objects.filter(
                        client_id=job.client_id, pk__in=list(answers.keys())
                    )
                }
                ScreeningAnswer.objects.bulk_create(
                    [
                        ScreeningAnswer(
                            client_id=job.client_id,
                            application=application,
                            question=question,
                            answer=str(answers.get(question_id, "")),
                        )
                        for question_id, question in questions.items()
                    ]
                )

        return Response(
            {"received": True, "reference": str(application.id)},
            status=status.HTTP_201_CREATED,
        )


class PublicUploadUrlView(PublicWriteView):
    """A résumé upload slot for the careers portal.

    Scoped to ``resume`` only, so an unauthenticated caller cannot mint an
    upload URL for a PMS proof or an HR document.
    """

    def post(self, request, job_id):
        from django.conf import settings as django_settings

        from apps.core import files as file_service
        from apps.core.models import File
        from apps.hrms.models import Job

        job = Job.objects.filter(
            is_published=True, status="Open", deleted_at__isnull=True
        ).filter(slug=job_id).first()
        if job is None:
            job = Job.objects.filter(
                pk=job_id, is_published=True, status="Open", deleted_at__isnull=True
            ).first()
        if job is None:
            raise NotFound("This role is no longer open.")

        content_type, size = file_service.validate_upload_request(
            file_name=request.data.get("fileName"),
            content_type=request.data.get("contentType"),
            size=request.data.get("size"),
            scope="resume",
        )
        row = File.objects.create(
            client_id=job.client_id,
            scope="resume",
            storage_key=file_service.build_storage_key(
                job.client_id, "resume", request.data.get("fileName")
            ),
            file_name=request.data.get("fileName"),
            content_type=content_type,
            file_size=size,
            status="pending",
        )
        token = file_service.sign_upload(row.id)
        return Response(
            {
                "fileId": str(row.id),
                "uploadUrl": request.build_absolute_uri(
                    f"{django_settings.API_BASE_PATH}/files/{row.id}/upload/?token={token}"
                ),
                "expiresAt": timezone.now()
                + timedelta(seconds=django_settings.UPLOAD_URL_TTL_SECONDS),
            },
            status=status.HTTP_201_CREATED,
        )
