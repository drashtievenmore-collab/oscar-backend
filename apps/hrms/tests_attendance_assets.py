"""Asset requests, the attendance register and the attendance -> leave -> payroll chain."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.hrms import services
from apps.hrms.models import Asset, AssetRequest, Attendance, Employee, LeaveType, Payslip


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class HrmsFlowTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("hr"), name="HR Tenant")
        sync_permissions()
        seed_roles(self.tenant)

        self.hr = self.login_for("HR Manager", "edit_staff", "view_staff", "view_team_attendance",
                                 "mark_attendance", "approve_leave", "apply_leave",
                                 "generate_payroll", "approve_payroll", "view_own_payslip")
        self.staff = self.login_for("Ravi Weaver")
        self.owner = User.objects.create_user(
            email=f"{uid('own')}@example.com", password="pass", client=self.tenant,
            name="Owner", role=Role.objects.get(client=self.tenant, code="OWNER"),
        )
        self.owner_employee = services.sync_employee_from_user(self.owner)
        for employee in Employee.objects.filter(client=self.tenant):
            employee.standard_salary = Decimal("26000")
            employee.joining_date = date(2026, 1, 1)
            employee.save()

    def login_for(self, name, *permission_ids):
        role = Role.objects.create(client=self.tenant, code=uid("ROLE"), name=uid("Role"))
        for permission_id in permission_ids:
            RolePermission.objects.create(role=role, permission_id=permission_id)
        user = User.objects.create_user(
            email=f"{uid('u')}@example.com", password="pass", client=self.tenant,
            name=name, role=role,
        )
        services.sync_employee_from_user(user)
        user.refresh_from_db()
        return user

    def api_for(self, user):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return api

    # -- who is listed ---------------------------------------------------------
    def test_admin_logins_stay_out_of_workforce_lists(self):
        api = self.api_for(self.hr)
        names = {row["name"] for row in api.get("/api/v1/hrms/employees/").json()["results"]}
        self.assertIn("Ravi Weaver", names)
        self.assertNotIn("Owner", names)

        daily = api.get("/api/v1/hrms/attendance/daily/", {"date": "2026-10-21"}).json()
        daily_names = {row["name"] for row in daily["results"]}
        self.assertIn("Ravi Weaver", daily_names)
        self.assertNotIn("Owner", daily_names)
        self.assertEqual(daily["summary"]["notMarked"], daily["summary"]["total"])

    # -- assets ----------------------------------------------------------------
    def make_asset(self, name="ThinkPad T14", status="Available"):
        return Asset.objects.create(client=self.tenant, asset_code=uid("AST"), name=name,
                                    status=status)

    def test_employee_requests_an_inventory_asset_and_hr_issues_it(self):
        laptop = self.make_asset()
        self.make_asset("Broken Monitor", status="Under Maintenance")
        staff_api = self.api_for(self.staff)

        stock = staff_api.get("/api/v1/hrms/assets/available/").json()["results"]
        self.assertEqual([row["name"] for row in stock], ["ThinkPad T14"])

        created = staff_api.post("/api/v1/hrms/asset-requests/", {
            "requestType": "Inventory", "requestedAssetId": str(laptop.id),
            "priority": "High", "justification": "Design work",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["itemName"], "ThinkPad T14")

        duplicate = staff_api.post("/api/v1/hrms/asset-requests/", {
            "requestType": "Inventory", "requestedAssetId": str(laptop.id),
            "justification": "Again",
        }, format="json")
        self.assertEqual(duplicate.status_code, 409)

        approved = self.api_for(self.hr).post(
            f"/api/v1/hrms/asset-requests/{created.json()['id']}/approve/", {}, format="json"
        )
        self.assertEqual(approved.status_code, 200, approved.content)
        self.assertEqual(approved.json()["status"], "Fulfilled")
        laptop.refresh_from_db()
        self.assertEqual(laptop.status, "Assigned")
        self.assertEqual(laptop.assigned_employee_id, self.staff.employee_id)

    def test_unavailable_asset_cannot_be_requested(self):
        repair = self.make_asset("Old Printer", status="Under Repair")
        response = self.api_for(self.staff).post("/api/v1/hrms/asset-requests/", {
            "requestType": "Inventory", "requestedAssetId": str(repair.id),
            "justification": "Printing",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_other_request_is_approved_then_fulfilled(self):
        staff_api = self.api_for(self.staff)
        created = staff_api.post("/api/v1/hrms/asset-requests/", {
            "requestType": "Other", "itemName": "Safety goggles", "categoryName": "Safety",
            "justification": "Loom floor",
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["category"], "Safety")
        request_id = created.json()["id"]

        hr_api = self.api_for(self.hr)
        self.assertEqual(
            hr_api.post(f"/api/v1/hrms/asset-requests/{request_id}/approve/", {}).json()["status"],
            "Approved",
        )
        goggles = self.make_asset("Safety goggles")
        fulfilled = hr_api.post(f"/api/v1/hrms/asset-requests/{request_id}/fulfill/",
                                {"assetId": str(goggles.id)}, format="json")
        self.assertEqual(fulfilled.json()["status"], "Fulfilled")

        # Employees only ever see their own requests.
        self.assertEqual(len(staff_api.get("/api/v1/hrms/asset-requests/").json()["results"]), 1)
        self.assertEqual(
            staff_api.post(f"/api/v1/hrms/asset-requests/{request_id}/approve/").status_code, 403
        )

    # -- attendance -> leave -> payroll -----------------------------------------
    def test_register_leave_and_payroll_agree(self):
        hr_api = self.api_for(self.hr)
        employee = Employee.objects.get(pk=self.staff.employee_id)
        month = date(2026, 9, 1)
        working = services.working_days_in(self.tenant.id, month, date(2026, 9, 30))

        # Present every working day except: one paid leave, one unpaid leave, one absence.
        paid_day, unpaid_day, absent_day = working[3], working[4], working[5]
        records = [
            {"employeeId": str(employee.id), "date": d.isoformat(), "status": "Present",
             "checkIn": "09:20", "checkOut": "18:30"}
            for d in working if d not in (paid_day, unpaid_day, absent_day)
        ]
        records.append({"employeeId": str(employee.id), "date": absent_day.isoformat(),
                        "status": "Absent"})
        bulk = hr_api.post("/api/v1/hrms/attendance/bulk/", {"records": records}, format="json")
        self.assertEqual(bulk.status_code, 200, bulk.content)
        self.assertEqual(bulk.json()["written"], len(records))
        row = Attendance.objects.get(employee=employee, work_date=working[0])
        self.assertEqual(row.status, "Present")
        self.assertEqual(row.hours, Decimal("9.17"))

        staff_api = self.api_for(self.staff)
        for leave_type, day in (("Casual Leave", paid_day), ("Loss of Pay", unpaid_day)):
            applied = staff_api.post("/api/v1/hrms/leave/", {
                "leaveTypeName": leave_type, "fromDate": day.isoformat(),
                "toDate": day.isoformat(), "reason": "Family",
            }, format="json")
            self.assertEqual(applied.status_code, 201, applied.content)
            approved = hr_api.patch(f"/api/v1/hrms/leave/{applied.json()['id']}/",
                                    {"status": "Approved"}, format="json")
            self.assertEqual(approved.status_code, 200, approved.content)

        balances = staff_api.get("/api/v1/hrms/leave/balances/summary/").json()["balances"]
        casual = next(b for b in balances if b["leaveType"] == "Casual Leave")
        self.assertEqual((casual["total"], casual["used"]), (12.0, 1.0))

        sheet = hr_api.get(f"/api/v1/hrms/attendance/individual/{employee.id}/",
                           {"month": "2026-09"}).json()
        self.assertEqual(sheet["summary"]["paidLeave"], 1.0)
        self.assertEqual(sheet["summary"]["unpaidLeave"], 1.0)
        self.assertEqual(sheet["summary"]["absent"], 1.0)

        processed = hr_api.post("/api/v1/hrms/payroll/process/", {"month": "2026-09-01"},
                                format="json")
        self.assertEqual(processed.status_code, 201, processed.content)
        names = {row["name"] for row in processed.json()["payslips"]}
        self.assertNotIn("Owner", names)

        payslip = Payslip.objects.get(employee=employee, period_month=month)
        self.assertEqual(payslip.total_days, Decimal(len(working)))
        self.assertEqual(payslip.attended_days, Decimal(len(working) - 3))
        self.assertEqual(payslip.paid_leaves, Decimal("1"))
        per_day = Decimal("26000") / Decimal(len(working))
        self.assertEqual(payslip.earned_salary, (Decimal("26000") - per_day * 2).quantize(Decimal("0.01")))

        listed = hr_api.get("/api/v1/hrms/payroll/", {"period": "2026-09"}).json()["results"]
        mine = next(row for row in listed if row["employeeId"] == str(employee.id))
        self.assertEqual(mine["attendance"]["unpaidLeave"], 1.0)
        self.assertEqual(mine["breakdown"]["absentDays"], 2.0)

    def test_flexibility_labels_drive_the_engine(self):
        hr_api = self.api_for(self.hr)
        saved = hr_api.put("/api/v1/hrms/attendance/flexibility/", {
            "gracePeriod": "5 minutes", "flexibleWorkingHours": "10:00 - 19:00 (Fixed)",
            "halfDayThreshold": "5 hours", "minimumWorkingHours": "9 hours",
        }, format="json").json()
        self.assertEqual((saved["graceMinutes"], saved["shiftStart"], saved["shiftEnd"]),
                         (5, "10:00", "19:00"))
        self.assertEqual(saved["lateAfter"], "10:05")
        verdict, _ = services.derive_attendance(
            self.tenant.id,
            check_in=timezone.make_aware(timezone.datetime(2026, 9, 1, 10, 4)),
            check_out=timezone.make_aware(timezone.datetime(2026, 9, 1, 19, 30)),
        )
        self.assertEqual(verdict, "Present")
