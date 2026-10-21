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
            ("approve_payment", "Approve payment out / voucher"),
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
            ("menu_jobwork", "Job Work menu"),
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
            ("approve_purchase_order", "Approve purchase order"),
            ("receive_goods", "Receive goods"),
            ("approve_qc", "Approve QC"),
            ("create_bill", "Create bill"),
            ("approve_bill_mismatch", "Approve vendor bill mismatch"),
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
    (
        "Job Work",
        "Job Work Operations",
        [
            ("view_jobwork", "View job work"),
            ("manage_jobwork", "Manage job work"),
            ("approve_jobwork", "Approve job work"),
        ],
    ),
    (
        "Masters",
        "Textile & Production Masters",
        [
            ("view_masters", "View masters"),
            ("manage_agencies", "Manage production agencies"),
            ("manage_brokers", "Manage brokers"),
            ("manage_processes", "Manage processes"),
            ("manage_jobwork_rates", "Manage job work rates"),
            ("manage_defects", "Manage defects"),
            ("manage_loss_reasons", "Manage loss reasons"),
            ("manage_return_reasons", "Manage return reasons"),
        ],
    ),
    (
        "Control & Compliance",
        "Approvals & Exceptions",
        [
            ("view_approvals", "View approval requests"),
            ("manage_approvals", "Approve or reject requests"),
            ("return_approval", "Return transaction for correction"),
            ("manage_approval_rules", "Configure approval rules & limits"),
            ("view_alerts", "View system alerts & exceptions"),
            ("manage_alerts", "Acknowledge and configure alerts"),
            ("view_owner_mis", "View executive and owner MIS dashboards"),
            ("manage_samples", "Issue, return, and consume samples"),
            ("manage_logistics", "Dispatch tempo trips and allocate freight"),
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


#: Seed roles for a fresh tenant. The 12 core Phase 4 roles (PDF §16).
DEFAULT_ROLES = [
    (
        "OWNER",
        "Owner / Management",
        "Full uninhibited access to all modules, financial ledgers, settings and approvals.",
        ["*"],
    ),
    (
        "SYS_ADMIN",
        "System Administrator",
        "Administrative configuration, user management, and system setup without unrestricted transactional approvals.",
        [
            "menu_admin",
            "view_staff",
            "create_staff",
            "edit_staff",
            "delete_staff",
            "manage_roles",
            "reset_staff_password",
            "view_audit_logs",
            "system_backup",
            "manage_company_profile",
            "view_alerts",
            "manage_alerts",
            "view_approvals",
        ],
    ),
    (
        "AD",
        "Admin",
        "Administrative configuration and user management.",
        [
            "menu_admin",
            "view_staff",
            "create_staff",
            "edit_staff",
            "delete_staff",
            "manage_roles",
            "reset_staff_password",
            "view_audit_logs",
            "system_backup",
            "manage_company_profile",
            "view_alerts",
            "manage_alerts",
            "view_approvals",
        ],
    ),
    (
        "PURCHASE_OFFICER",
        "Purchase Officer",
        "Purchase order drafts creation, supplier deliveries, and goods receipts. Cannot approve own restricted orders.",
        [
            "menu_purchase",
            "menu_inventory",
            "view_purchase",
            "create_purchase_order",
            "receive_goods",
            "view_inventory",
            "view_masters",
            "export_excel",
        ],
    ),
    (
        "PURCHASE_MGR",
        "Purchase Manager",
        "Procurement, purchase order approvals, supplier reconciliation, and rate exception approvals.",
        [
            "menu_purchase",
            "menu_inventory",
            "menu_jobwork",
            "view_purchase",
            "create_purchase_order",
            "approve_purchase_order",
            "receive_goods",
            "approve_qc",
            "create_bill",
            "record_payment_out",
            "cancel_purchase_document",
            "view_inventory",
            "adjust_stock",
            "create_transfer",
            "view_jobwork",
            "manage_jobwork",
            "approve_jobwork",
            "view_masters",
            "manage_agencies",
            "manage_brokers",
            "manage_processes",
            "manage_jobwork_rates",
            "manage_defects",
            "manage_loss_reasons",
            "manage_return_reasons",
            "view_approvals",
            "manage_approvals",
            "return_approval",
            "view_alerts",
            "export_excel",
        ],
    ),
    (
        "STORE_INCHARGE",
        "Store / Godown Incharge",
        "Goods receipt (GRN), lot/taka registry, tag printing, stock transfers, and warehouse audit.",
        [
            "menu_inventory",
            "menu_purchase",
            "view_inventory",
            "adjust_stock",
            "create_transfer",
            "approve_zone_request",
            "perform_audit",
            "receive_goods",
            "view_purchase",
            "manage_samples",
            "manage_logistics",
            "view_approvals",
            "view_alerts",
            "export_excel",
        ],
    ),
    (
        "INV_MGR",
        "Godown / Inventory Manager",
        "Goods receipt (GRN), lot/taka registry, tag printing, stock transfers, and warehouse audit.",
        [
            "menu_inventory",
            "menu_purchase",
            "view_inventory",
            "adjust_stock",
            "create_transfer",
            "approve_zone_request",
            "perform_audit",
            "receive_goods",
            "view_purchase",
            "manage_samples",
            "manage_logistics",
            "view_approvals",
            "view_alerts",
            "export_excel",
        ],
    ),
    (
        "PROD_COORD",
        "Production Coordinator",
        "Production work instructions, external agency assignments, daily entries, and completion verification.",
        [
            "menu_pms",
            "menu_jobwork",
            "view_pms",
            "view_production",
            "enter_production",
            "verify_production",
            "view_jobwork",
            "manage_jobwork",
            "approve_jobwork",
            "view_masters",
            "view_inventory",
            "view_approvals",
            "view_alerts",
            "export_excel",
        ],
    ),
    (
        "PC",
        "Production Coordinator",
        "Production work instructions, external agency assignments, daily entries, and completion verification.",
        [
            "menu_pms",
            "menu_jobwork",
            "view_pms",
            "view_production",
            "enter_production",
            "verify_production",
            "view_jobwork",
            "manage_jobwork",
            "approve_jobwork",
            "view_masters",
            "view_inventory",
            "view_approvals",
            "view_alerts",
            "export_excel",
        ],
    ),
    (
        "PROD_STAFF",
        "Production Employee / Field Staff",
        "Assigned work instructions, daily production entries, output and defect tracking.",
        [
            "menu_pms",
            "view_pms",
            "view_production",
            "enter_production",
        ],
    ),
    (
        "QC_INSPECTOR",
        "QC Inspector",
        "Grey fabric and job work quality inspection, piece-by-piece testing, and pass/hold/reject disposition.",
        [
            "menu_inventory",
            "view_inventory",
            "approve_qc",
            "view_masters",
            "manage_defects",
            "manage_loss_reasons",
            "manage_return_reasons",
            "view_approvals",
            "view_alerts",
        ],
    ),
    (
        "QC_PERSON",
        "QC Inspector",
        "Grey fabric and job work quality inspection, piece-by-piece testing, and pass/hold/reject disposition.",
        [
            "menu_inventory",
            "view_inventory",
            "approve_qc",
            "view_masters",
            "manage_defects",
            "manage_loss_reasons",
            "manage_return_reasons",
            "view_approvals",
            "view_alerts",
        ],
    ),
    (
        "JOBWORK_MGR",
        "Job Work Manager",
        "Process plans, dyeing/printing job orders, inward/outward, vendor reconciliation, and final job work POs.",
        [
            "menu_jobwork",
            "menu_purchase",
            "menu_inventory",
            "view_jobwork",
            "manage_jobwork",
            "approve_jobwork",
            "view_purchase",
            "create_purchase_order",
            "view_inventory",
            "view_masters",
            "manage_agencies",
            "manage_brokers",
            "manage_processes",
            "manage_jobwork_rates",
            "view_approvals",
            "manage_approvals",
            "return_approval",
            "view_alerts",
            "export_excel",
        ],
    ),
    (
        "ACCOUNTS_EXEC",
        "Accounts Executive",
        "Supplier bills, 3-way matching, payment vouchers, cash/bank transactions, and general ledger entry.",
        [
            "menu_accounts",
            "menu_sales",
            "menu_purchase",
            "view_bank_accounts",
            "manage_journal_entries",
            "view_ledger",
            "view_financial_reports",
            "view_sales",
            "view_purchase",
            "create_bill",
            "record_payment_in",
            "record_payment_out",
            "show_account_dashboard",
            "export_excel",
        ],
    ),
    (
        "ACCOUNTS_MGR",
        "Accounts Manager",
        "Financial approvals, bill mismatch approvals, payment authorization, ledgers, and financial reporting.",
        [
            "menu_accounts",
            "menu_sales",
            "menu_purchase",
            "view_bank_accounts",
            "manage_journal_entries",
            "view_ledger",
            "view_financial_reports",
            "reconcile_bank",
            "view_sales",
            "view_purchase",
            "create_bill",
            "record_payment_in",
            "record_payment_out",
            "finalize_invoice",
            "approve_bill_mismatch",
            "approve_payment",
            "show_account_dashboard",
            "view_approvals",
            "manage_approvals",
            "return_approval",
            "view_alerts",
            "view_owner_mis",
            "export_excel",
        ],
    ),
    (
        "ACCOUNTS",
        "Accounts Manager",
        "Financial approvals, bill mismatch approvals, payment authorization, ledgers, and financial reporting.",
        [
            "menu_accounts",
            "menu_sales",
            "menu_purchase",
            "view_bank_accounts",
            "manage_journal_entries",
            "view_ledger",
            "view_financial_reports",
            "reconcile_bank",
            "view_sales",
            "view_purchase",
            "create_bill",
            "record_payment_in",
            "record_payment_out",
            "finalize_invoice",
            "approve_bill_mismatch",
            "approve_payment",
            "show_account_dashboard",
            "view_approvals",
            "manage_approvals",
            "return_approval",
            "view_alerts",
            "view_owner_mis",
            "export_excel",
        ],
    ),
    (
        "SALES_MGR",
        "Sales Manager",
        "Sales pipeline, quotations, orders, credit checks, pricing, broker commission, and discount approvals.",
        [
            "menu_sales",
            "menu_crm",
            "menu_inventory",
            "view_sales",
            "create_quotation",
            "create_sales_order",
            "create_invoice",
            "finalize_invoice",
            "cancel_invoice",
            "record_payment_in",
            "view_lead",
            "create_lead",
            "edit_lead",
            "move_lead",
            "manage_pipeline",
            "view_task",
            "create_task",
            "edit_task",
            "assign_task",
            "view_inventory",
            "show_crm_dashboard",
            "manage_samples",
            "view_approvals",
            "manage_approvals",
            "return_approval",
            "view_alerts",
            "export_excel",
        ],
    ),
    (
        "SALES_EXEC",
        "Sales / CRM Executive",
        "Sales pipeline, customer quotations, sales orders, leads, and customer reservation records.",
        [
            "menu_sales",
            "menu_crm",
            "menu_inventory",
            "view_sales",
            "create_quotation",
            "create_sales_order",
            "view_lead",
            "create_lead",
            "edit_lead",
            "view_task",
            "create_task",
            "view_inventory",
            "export_excel",
        ],
    ),
    (
        "LOGISTICS",
        "Logistics Coordinator",
        "Dispatch tracking, vehicle trips, freight allocation, delivery challans, and stock transfers.",
        [
            "menu_sales",
            "menu_inventory",
            "view_sales",
            "create_sales_order",
            "view_inventory",
            "create_transfer",
            "manage_logistics",
            "view_approvals",
            "view_alerts",
            "export_excel",
        ],
    ),
    (
        "AUDITOR",
        "Auditor / Read-only User",
        "Read-only audit visibility across all modules, registers, ledgers, vouchers, and MIS reports without mutation rights.",
        [
            "menu_sales",
            "menu_purchase",
            "menu_inventory",
            "menu_accounts",
            "menu_jobwork",
            "menu_hrms",
            "menu_pms",
            "menu_admin",
            "view_sales",
            "view_purchase",
            "view_inventory",
            "view_ledger",
            "view_financial_reports",
            "view_jobwork",
            "view_production",
            "view_staff",
            "view_owner_mis",
            "view_approvals",
            "view_alerts",
            "export_excel",
        ],
    ),
    (
        "HR",
        "HR / Payroll",
        "Employees, attendance, leave and payroll processing.",
        [
            "menu_hrms",
            "mark_attendance",
            "view_team_attendance",
            "approve_leave",
            "regularize_attendance",
            "generate_payroll",
            "approve_payroll",
            "edit_salary_structure",
            "view_staff",
            "create_staff",
            "edit_staff",
            "show_hrm_dashboard",
            "view_approvals",
            "view_alerts",
            "export_excel",
        ],
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
