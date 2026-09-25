"""
The RBAC permission catalogue (api.md Appendix B).

``GET /admin/permissions/`` returns this grouped exactly as the UI renders it:
modules -> groups -> permissions. The seed lives here rather than in the
frontend's ``DEFAULT_MODULE_PERMISSIONS`` constant, which api-integration.md
§9.4 keeps only as an offline-dev fallback.

The block after "Gaps to fill" in Appendix B is included: Appendix B shipped
with no ids for Sales/Purchase/Inventory document actions or for PMS stage
handoff and approval, and api.md asks for them in the same structure.
"""

#: (module, group, [(permission_id, label), ...])
CATALOGUE = [
    (
        "CRM",
        "CRM Dashboard",
        [
            ("show_crm_dashboard", "Show CRM dashboard"),
            ("show_hrm_dashboard", "Show HRM dashboard"),
            ("show_account_dashboard", "Show account dashboard"),
            ("show_templates_menu", "Show templates menu"),
        ],
    ),
    (
        "CRM",
        "Lead Management",
        [
            ("create_lead", "Create lead"),
            ("view_lead", "View lead"),
            ("edit_lead", "Edit lead"),
            ("delete_lead", "Delete lead"),
            ("move_lead", "Move lead between stages"),
        ],
    ),
    (
        "CRM",
        "Pipeline & Stage",
        [
            ("manage_pipeline", "Manage pipeline"),
            ("create_pipeline", "Create pipeline"),
            ("edit_pipeline", "Edit pipeline"),
            ("delete_pipeline", "Delete pipeline"),
        ],
    ),
    (
        "CRM",
        "Tasks",
        [
            ("view_task", "View task"),
            ("create_task", "Create task"),
            ("edit_task", "Edit task"),
            ("delete_task", "Delete task"),
            ("assign_task", "Assign task"),
            ("manage_task_allocation", "Manage task allocation"),
        ],
    ),
    (
        "Staff",
        "Staff & User Access",
        [
            ("view_staff", "View staff"),
            ("create_staff", "Create staff"),
            ("edit_staff", "Edit staff"),
            ("delete_staff", "Delete staff"),
            ("manage_roles", "Manage roles"),
            ("reset_staff_password", "Reset staff password"),
        ],
    ),
    (
        "Project",
        "Projects & Milestones",
        [
            ("view_projects", "View projects"),
            ("create_project", "Create project"),
            ("edit_project", "Edit project"),
            ("delete_project", "Delete project"),
            ("manage_milestones", "Manage milestones"),
            ("assign_members", "Assign members"),
        ],
    ),
    (
        "HRM",
        "Attendance & Leave",
        [
            ("mark_attendance", "Mark attendance"),
            ("view_team_attendance", "View team attendance"),
            ("apply_leave", "Apply for leave"),
            ("approve_leave", "Approve leave"),
            ("regularize_attendance", "Regularize attendance"),
        ],
    ),
    (
        "HRM",
        "Payroll & Compensation",
        [
            ("view_own_payslip", "View own payslip"),
            ("generate_payroll", "Generate payroll"),
            ("edit_salary_structure", "Edit salary structure"),
            ("approve_payroll", "Approve payroll"),
        ],
    ),
    (
        "Account",
        "General Ledger & Accounts",
        [
            ("view_bank_accounts", "View bank accounts"),
            ("manage_journal_entries", "Manage journal entries"),
            ("view_ledger", "View ledger"),
            ("view_financial_reports", "View financial reports"),
            ("reconcile_bank", "Reconcile bank"),
        ],
    ),
    (
        "POS",
        "Point of Sale & Invoicing",
        [
            ("create_pos_invoice", "Create POS invoice"),
            ("view_pos_orders", "View POS orders"),
            ("apply_discounts", "Apply discounts"),
            ("process_returns", "Process returns"),
            ("print_receipts", "Print receipts"),
        ],
    ),
    (
        "Menu Access",
        "Navigation Visibility",
        [
            ("menu_crm", "CRM menu"),
            ("menu_sales", "Sales menu"),
            ("menu_purchase", "Purchase menu"),
            ("menu_inventory", "Inventory menu"),
            ("menu_accounts", "Accounts menu"),
            ("menu_hrms", "HRMS menu"),
            ("menu_admin", "Administration menu"),
            ("menu_pms", "PMS menu"),
        ],
    ),
    (
        "Other Modules",
        "System Utilities & Tools",
        [
            ("export_excel", "Export to Excel"),
            ("view_audit_logs", "View audit logs"),
            ("system_backup", "System backup"),
            ("manage_company_profile", "Manage company profile"),
        ],
    ),
    # ---- api.md Appendix B, "Gaps to fill" --------------------------------
    (
        "Sales",
        "Sales Documents",
        [
            ("view_sales", "View sales"),
            ("create_quotation", "Create quotation"),
            ("create_sales_order", "Create sales order"),
            ("create_invoice", "Create invoice"),
            ("finalize_invoice", "Finalize invoice"),
            ("cancel_invoice", "Cancel invoice"),
            ("record_payment_in", "Record payment in"),
            ("override_credit_limit", "Override credit limit"),
        ],
    ),
    (
        "Purchase",
        "Purchase Documents",
        [
            ("view_purchase", "View purchase"),
            ("create_purchase_order", "Create purchase order"),
            ("receive_goods", "Receive goods"),
            ("approve_qc", "Approve QC"),
            ("create_bill", "Create bill"),
            ("record_payment_out", "Record payment out"),
            ("cancel_purchase_document", "Cancel purchase document"),
        ],
    ),
    (
        "Inventory",
        "Stock Operations",
        [
            ("view_inventory", "View inventory"),
            ("adjust_stock", "Adjust stock"),
            ("create_transfer", "Create transfer"),
            ("approve_zone_request", "Approve zone request"),
            ("perform_audit", "Perform audit"),
        ],
    ),
    (
        "PMS",
        "Project Management",
        [
            ("view_pms", "View PMS"),
            ("create_pms_project", "Create PMS project"),
            ("assign_stage", "Assign stage"),
            ("handoff_stage", "Handoff stage"),
            ("approve_document", "Approve document"),
            ("share_client_proof", "Share client proof"),
            ("log_delay", "Log delay"),
            ("complete_project", "Complete project"),
        ],
    ),
    (
        "Production",
        "Production Monitoring",
        [
            ("view_production", "View production"),
            ("enter_production", "Enter production"),
            ("verify_production", "Verify production"),
        ],
    ),
]


def iter_permissions():
    """Yield ``(id, module, group, label, sort_order)`` in catalogue order."""
    sort_order = 0
    for module, group, permissions in CATALOGUE:
        for permission_id, label in permissions:
            yield permission_id, module, group, label, sort_order
            sort_order += 10


def all_permission_ids():
    return [permission_id for permission_id, *_ in iter_permissions()]


def sync_permissions():
    """Upsert the catalogue. Idempotent -- safe to run on every deploy."""
    from .models import Permission

    existing = {row.id: row for row in Permission.objects.all()}
    to_create, to_update = [], []
    for permission_id, module, group, label, sort_order in iter_permissions():
        row = existing.get(permission_id)
        if row is None:
            to_create.append(
                Permission(
                    id=permission_id,
                    module=module,
                    group=group,
                    label=label,
                    sort_order=sort_order,
                )
            )
        elif (row.module, row.group, row.label, row.sort_order) != (
            module,
            group,
            label,
            sort_order,
        ):
            row.module, row.group, row.label, row.sort_order = module, group, label, sort_order
            to_update.append(row)

    if to_create:
        Permission.objects.bulk_create(to_create)
    if to_update:
        Permission.objects.bulk_update(to_update, ["module", "group", "label", "sort_order"])
    return len(to_create), len(to_update)


def grouped_catalogue():
    """The exact structure ``GET /admin/permissions/`` returns.

    modules -> groups -> permissions, read from the table so a tenant that has
    added a permission sees it without a frontend release.
    """
    from .models import Permission

    modules = []
    index = {}
    for row in Permission.objects.all().order_by("module", "sort_order", "id"):
        module = index.get(row.module)
        if module is None:
            module = {"module": row.module, "groups": []}
            index[row.module] = module
            modules.append(module)
        group = next((g for g in module["groups"] if g["group"] == row.group), None)
        if group is None:
            group = {"group": row.group, "permissions": []}
            module["groups"].append(group)
        group["permissions"].append(
            {"id": row.id, "label": row.label, "description": row.description}
        )
    return modules


#: Seed roles for a fresh tenant. ``"*"`` means every permission in the
#: catalogue -- the Administrator role.
DEFAULT_ROLES = [
    ("AD", "Administrator", "Full access to every module.", ["*"]),
    (
        "SM",
        "Sales Manager",
        "Owns the sales pipeline and the CRM.",
        [
            "menu_sales", "menu_crm", "menu_inventory",
            "view_sales", "create_quotation", "create_sales_order", "create_invoice",
            "finalize_invoice", "cancel_invoice", "record_payment_in",
            "view_lead", "create_lead", "edit_lead", "move_lead", "manage_pipeline",
            "view_task", "create_task", "edit_task", "assign_task",
            "view_inventory", "show_crm_dashboard", "export_excel",
        ],
    ),
    (
        "AC",
        "Accountant",
        "Ledgers, payments and financial reporting.",
        [
            "menu_accounts", "menu_sales", "menu_purchase",
            "view_bank_accounts", "manage_journal_entries", "view_ledger",
            "view_financial_reports", "reconcile_bank",
            "view_sales", "view_purchase", "record_payment_in", "record_payment_out",
            "finalize_invoice", "show_account_dashboard", "export_excel",
        ],
    ),
    (
        "PU",
        "Purchase Officer",
        "Procurement, goods receipt and QC.",
        [
            "menu_purchase", "menu_inventory",
            "view_purchase", "create_purchase_order", "create_bill", "receive_goods",
            "approve_qc", "record_payment_out", "cancel_purchase_document",
            "view_inventory", "adjust_stock", "create_transfer",
        ],
    ),
    (
        "ST",
        "Store Keeper",
        "Stock position, transfers and audits.",
        [
            "menu_inventory", "view_inventory", "adjust_stock", "create_transfer",
            "approve_zone_request", "perform_audit", "receive_goods",
        ],
    ),
    (
        "PM",
        "Project Manager",
        "PMS projects, stages and client approvals.",
        [
            "menu_pms", "menu_sales",
            "view_pms", "create_pms_project", "assign_stage", "handoff_stage",
            "approve_document", "share_client_proof", "log_delay", "complete_project",
            "view_projects", "create_project", "edit_project", "manage_milestones",
            "assign_members", "view_sales",
        ],
    ),
    (
        "HR",
        "HR Manager",
        "Employees, attendance, leave and payroll.",
        [
            "menu_hrms",
            "mark_attendance", "view_team_attendance", "approve_leave",
            "regularize_attendance", "generate_payroll", "approve_payroll",
            "edit_salary_structure", "view_staff", "create_staff", "edit_staff",
            "show_hrm_dashboard", "export_excel",
        ],
    ),
    (
        "PC",
        "Production Coordinator",
        "Grey-fabric monitoring and verification. No payroll write.",
        [
            "menu_hrms",
            "view_staff",
            "view_production", "enter_production", "verify_production",
            "export_excel",
        ],
    ),
    (
        "EM",
        "Employee",
        "Self-service access only.",
        ["apply_leave", "view_own_payslip", "mark_attendance", "view_task"],
    ),
]


def seed_roles(client):
    """Create the default roles for a tenant. Idempotent."""
    from .models import Permission, Role, RolePermission

    every_permission = list(Permission.objects.values_list("id", flat=True))
    created_roles = []
    for code, name, description, permission_ids in DEFAULT_ROLES:
        role, created = Role.objects.get_or_create(
            client=client,
            code=code,
            defaults={"name": name, "description": description, "is_system": True},
        )
        if not created:
            continue
        created_roles.append(role)
        wanted = every_permission if permission_ids == ["*"] else permission_ids
        RolePermission.objects.bulk_create(
            [
                RolePermission(role=role, permission_id=permission_id)
                for permission_id in wanted
                if permission_id in set(every_permission)
            ],
            ignore_conflicts=True,
        )
    return created_roles
