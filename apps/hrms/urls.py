"""HRMS routes (api.md §11)."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("employees", views.EmployeeViewSet, basename="hrms-employees")
router.register("departments", views.HrmsDepartmentViewSet, basename="hrms-departments")
router.register("designations", views.DesignationViewSet, basename="hrms-designations")
router.register("locations", views.HrmsLocationViewSet, basename="hrms-locations")

# A router emits each prefix's list route *and* its detail route in the order
# the prefixes were registered, and a detail route matches any single segment.
# So `attendance/<pk>/` would swallow `attendance/regularizations/` — the nested
# collections have to be registered before the parent they sit under.
router.register(
    "attendance/regularizations",
    views.RegularizationViewSet,
    basename="hrms-regularizations",
)
router.register("attendance", views.AttendanceViewSet, basename="hrms-attendance")

router.register("leave/types", views.LeaveTypeViewSet, basename="hrms-leave-types")
router.register("leave/balances", views.LeaveBalanceViewSet, basename="hrms-leave-balances")
router.register(
    "leave/encashments", views.LeaveEncashmentViewSet, basename="hrms-leave-encashments"
)
router.register("leave", views.LeaveRequestViewSet, basename="hrms-leave")
router.register("comp-offs", views.CompOffViewSet, basename="hrms-comp-offs")

router.register(
    "payroll/salary-structures",
    views.SalaryStructureViewSet,
    basename="hrms-salary-structures",
)
router.register(
    "payroll/advances", views.SalaryAdvanceViewSet, basename="hrms-salary-advances"
)
router.register("payroll", views.PayslipViewSet, basename="hrms-payroll")
router.register("incentive-schemes", views.SalespersonIncentiveSchemeViewSet, basename="hrms-incentive-schemes")
router.register("incentives", views.SalespersonIncentiveViewSet, basename="hrms-incentives")
router.register("broker-commissions", views.BrokerCommissionViewSet, basename="hrms-broker-commissions")
router.register("transfers", views.StubLifecycleViewSet, basename="hrms-transfers")
router.register("promotions", views.StubLifecycleViewSet, basename="hrms-promotions")
router.register("warnings", views.StubLifecycleViewSet, basename="hrms-warnings")
router.register("awards", views.StubLifecycleViewSet, basename="hrms-awards")
router.register("jobs", views.StubLifecycleViewSet, basename="hrms-jobs")
router.register("candidates", views.StubLifecycleViewSet, basename="hrms-candidates")
router.register("applications", views.StubLifecycleViewSet, basename="hrms-applications")

# =============================================================================
# COMMENTED OUT PER OSCAR LETTER PAD SCOPE (Kept for reference, do not delete):
# =============================================================================
# router.register("jobs", views.JobViewSet, basename="hrms-jobs")
# router.register("candidates", views.CandidateViewSet, basename="hrms-candidates")
# router.register("applications", views.ApplicationViewSet, basename="hrms-applications")
# router.register("interviews", views.InterviewViewSet, basename="hrms-interviews")
# router.register("offers", views.OfferViewSet, basename="hrms-offers")
# router.register("onboarding", views.OnboardingViewSet, basename="hrms-onboarding")
# router.register("recruitment/questions", views.ScreeningQuestionViewSet, basename="hrms-screening-questions")
# router.register("meetings", views.CompanyMeetingViewSet, basename="hrms-meetings")
# router.register("meeting-rooms", views.MeetingRoomViewSet, basename="hrms-meeting-rooms")

router.register(
    "performance/cycles", views.AppraisalCycleViewSet, basename="hrms-appraisal-cycles"
)
router.register(
    "performance/indicators",
    views.PerformanceIndicatorViewSet,
    basename="hrms-performance-indicators",
)
router.register("performance/kpis", views.KpiViewSet, basename="hrms-kpis")
router.register(
    "performance/appraisals", views.AppraisalViewSet, basename="hrms-appraisals"
)
router.register("performance/goals", views.GoalViewSet, basename="hrms-goals")

router.register("trainings", views.TrainingViewSet, basename="hrms-trainings")
router.register("trainers", views.TrainerViewSet, basename="hrms-trainers")

router.register("assets", views.AssetViewSet, basename="hrms-assets")
router.register(
    "asset-categories", views.AssetCategoryViewSet, basename="hrms-asset-categories"
)
router.register("asset-requests", views.AssetRequestViewSet, basename="hrms-asset-requests")

router.register("documents", views.HrDocumentViewSet, basename="hrms-documents")
router.register("policies", views.PolicyViewSet, basename="hrms-policies")
router.register(
    "policy-categories", views.PolicyCategoryViewSet, basename="hrms-policy-categories"
)
router.register(
    "calendar/events", views.CalendarEventViewSet, basename="hrms-calendar-events"
)
router.register("holidays", views.HolidayViewSet, basename="hrms-holidays")

router.register("teams", views.TeamViewSet, basename="hrms-teams")
router.register(
    "approval-chains", views.ApprovalChainViewSet, basename="hrms-approval-chains"
)
router.register("terminations", views.TerminationViewSet, basename="hrms-terminations")
router.register("resignations", views.ResignationViewSet, basename="hrms-resignations")
router.register("complaints", views.ComplaintViewSet, basename="hrms-complaints")

urlpatterns = [
    path("org-chart/", views.OrgChartView.as_view(), name="hrms-org-chart"),
    path("dashboard/", views.HrmsDashboardView.as_view(), name="hrms-dashboard"),
    path(
        "attendance/flexibility/",
        views.FlexibilityPolicyView.as_view(),
        name="hrms-attendance-flexibility",
    ),
    # Recruitment views commented out per Oscar letter pad scope (kept for reference, do not remove):
    # path(
    #     "recruitment/funnel/",
    #     views.RecruitmentFunnelView.as_view(),
    #     {"section": "funnel"},
    #     name="hrms-recruitment-funnel",
    # ),
    # path(
    #     "recruitment/dashboard/",
    #     views.RecruitmentFunnelView.as_view(),
    #     name="hrms-recruitment-dashboard",
    # ),
    path(
        "performance/dashboard/",
        views.PerformanceDashboardView.as_view(),
        name="hrms-performance-dashboard",
    ),
    path("working-days/", views.WorkingDayView.as_view(), name="hrms-working-days"),
    path("settings/", views.HrmsSettingsView.as_view(), name="hrms-settings"),
    path("", include(router.urls)),
]
