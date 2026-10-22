"""Tests for HRMS recruitment route integration and reverse lookups."""

from django.test import TestCase
from django.urls import resolve, reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.accounts.models import Client, User
from apps.hrms.models import Job, Candidate, Application, Interview, Offer, OnboardingTask
from apps.hrms.views import (
    JobViewSet,
    CandidateViewSet,
    ApplicationViewSet,
    InterviewViewSet,
    OfferViewSet,
    OnboardingViewSet,
    ScreeningQuestionViewSet,
    RecruitmentFunnelView,
)


class RecruitmentRouteTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(name="Recruitment Tenant")
        self.user = User.objects.create(
            client=self.tenant,
            name="HR Admin",
            email="hr@recruitment.test",
            status="Active",
            is_staff=True,
            is_superuser=True,
        )
        self.factory = APIRequestFactory()

    def test_recruitment_url_reversals(self):
        self.assertEqual(reverse("hrms-candidates-list"), "/api/v1/hrms/candidates/")
        self.assertEqual(reverse("hrms-jobs-list"), "/api/v1/hrms/jobs/")
        self.assertEqual(reverse("hrms-applications-list"), "/api/v1/hrms/applications/")
        self.assertEqual(reverse("hrms-interviews-list"), "/api/v1/hrms/interviews/")
        self.assertEqual(reverse("hrms-offers-list"), "/api/v1/hrms/offers/")
        self.assertEqual(reverse("hrms-onboarding-list"), "/api/v1/hrms/onboarding/")
        self.assertEqual(reverse("hrms-screening-questions-list"), "/api/v1/hrms/recruitment/questions/")
        self.assertEqual(reverse("hrms-recruitment-funnel"), "/api/v1/hrms/recruitment/funnel/")
        self.assertEqual(reverse("hrms-recruitment-dashboard"), "/api/v1/hrms/recruitment/dashboard/")

    def test_candidates_list_endpoint(self):
        Candidate.objects.create(
            client=self.tenant,
            name="Alice Candidate",
            email="alice@test.invalid",
            phone="9876543210",
        )
        request = self.factory.get("/api/v1/hrms/candidates/")
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)

        view = CandidateViewSet.as_view({"get": "list"})
        response = view(request)
        self.assertEqual(response.status_code, 200)

    def test_jobs_list_endpoint(self):
        Job.objects.create(
            client=self.tenant,
            title="Senior Weaver",
        )
        request = self.factory.get("/api/v1/hrms/jobs/")
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)

        view = JobViewSet.as_view({"get": "list"})
        response = view(request)
        self.assertEqual(response.status_code, 200)

    def test_applications_list_endpoint(self):
        job = Job.objects.create(client=self.tenant, title="Spinning Supervisor")
        cand = Candidate.objects.create(client=self.tenant, name="Bob Applicant", email="bob@test.invalid")
        Application.objects.create(client=self.tenant, job=job, candidate=cand)

        request = self.factory.get("/api/v1/hrms/applications/")
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)

        view = ApplicationViewSet.as_view({"get": "list"})
        response = view(request)
        self.assertEqual(response.status_code, 200)

    def test_offers_list_endpoint(self):
        request = self.factory.get("/api/v1/hrms/offers/")
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)

        view = OfferViewSet.as_view({"get": "list"})
        response = view(request)
        self.assertEqual(response.status_code, 200)

    def test_onboarding_list_endpoint(self):
        cand = Candidate.objects.create(client=self.tenant, name="Charlie Hire", email="charlie@test.invalid")
        OnboardingTask.objects.create(client=self.tenant, candidate=cand, title="Collect Aadhaar Card")

        request = self.factory.get("/api/v1/hrms/onboarding/")
        request.client_id = self.tenant.id
        force_authenticate(request, user=self.user)

        view = OnboardingViewSet.as_view({"get": "list"})
        response = view(request)
        self.assertEqual(response.status_code, 200)
