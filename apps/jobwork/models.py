"""Job Work models — process plans and job work orders with nested lines."""
from django.db import models

from apps.core.models import LegacyIdMixin, TenantModel


class ProcessPlan(TenantModel, LegacyIdMixin):
    """A planned outside process (dyeing / printing / finishing / ...)."""

    STATUSES = [
        ("Active", "Active"),
        ("Draft", "Draft"),
        ("Completed", "Completed"),
    ]

    plan_no = models.TextField(db_index=True)
    date = models.DateField()
    process = models.TextField()
    process_category = models.TextField(null=True, blank=True)
    vendor_name = models.TextField(default="")
    contact_person = models.TextField(null=True, blank=True)
    phone = models.TextField(null=True, blank=True)
    email = models.TextField(null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    fabric_item = models.TextField(default="")
    grey_lot_no = models.TextField(null=True, blank=True)
    fabric_quality = models.TextField(default="")
    taka_roll_no = models.TextField(null=True, blank=True)
    shade = models.TextField(null=True, blank=True)
    expected_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    expected_loss = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    expected_return_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    weight_kg = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    finished_weight_kg = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    no_of_rolls = models.IntegerField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    expected_completion_date = models.DateField(null=True, blank=True)
    target_date = models.DateField(null=True, blank=True)
    assigned_employee = models.TextField(null=True, blank=True)
    approver = models.TextField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="Active")
    remarks = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "jobwork_process_plans"
        ordering = ["-date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "plan_no"], name="uq_jobwork_plan_no"
            )
        ]
        indexes = [
            models.Index(fields=["client", "-date"], name="ix_jobwork_plan_date"),
            models.Index(fields=["client", "status"], name="ix_jobwork_plan_status"),
        ]

    def __str__(self):
        return f"{self.plan_no} {self.process}"


class PlanCharge(TenantModel):
    process_plan = models.ForeignKey(
        ProcessPlan, on_delete=models.CASCADE, related_name="charges"
    )
    type = models.TextField(default="")
    description = models.TextField(null=True, blank=True)
    rate = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    status = models.TextField(default="Active")

    class Meta:
        db_table = "jobwork_plan_charges"
        ordering = ["created_at"]


class JobWorkOrder(TenantModel, LegacyIdMixin):
    """A job work order issued to a vendor for outside processing."""

    STATUSES = [
        ("Draft", "Draft"),
        ("In-Process", "In-Process"),
        ("Completed", "Completed"),
    ]

    jwo_no = models.TextField(db_index=True)
    process_plan = models.ForeignKey(
        ProcessPlan, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="job_work_orders",
    )
    process = models.TextField(default="")
    vendor_name = models.TextField(default="")
    vendor = models.ForeignKey(
        "masters.Party", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    order_date = models.DateField()
    planned_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    rate = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    expected_completion = models.DateField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="In-Process")
    remarks = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "jobwork_orders"
        ordering = ["-order_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "jwo_no"], name="uq_jobwork_jwo_no"
            )
        ]
        indexes = [
            models.Index(fields=["client", "-order_date"], name="ix_jobwork_order_date"),
            models.Index(fields=["client", "status"], name="ix_jobwork_order_status"),
        ]

    def __str__(self):
        return f"{self.jwo_no} {self.process}"


class JWOMaterial(TenantModel):
    job_work_order = models.ForeignKey(
        JobWorkOrder, on_delete=models.CASCADE, related_name="materials"
    )
    fabric_item = models.TextField(default="")
    fabric_quality = models.TextField(default="")
    shade = models.TextField(null=True, blank=True)
    lot_no = models.TextField(null=True, blank=True)
    qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    rate = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    class Meta:
        db_table = "jobwork_materials"
        ordering = ["created_at"]


class JWOOutward(TenantModel):
    job_work_order = models.ForeignKey(
        JobWorkOrder, on_delete=models.CASCADE, related_name="outwards"
    )
    no = models.TextField(default="")
    date = models.DateField()
    qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    lr_no = models.TextField(null=True, blank=True)
    transporter = models.TextField(null=True, blank=True)
    status = models.TextField(default="Sent")

    class Meta:
        db_table = "jobwork_outwards"
        ordering = ["-date", "-created_at"]


class JWOInward(TenantModel):
    job_work_order = models.ForeignKey(
        JobWorkOrder, on_delete=models.CASCADE, related_name="inwards"
    )
    no = models.TextField(default="")
    date = models.DateField()
    qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    accepted = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    rejected = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    challan_no = models.TextField(null=True, blank=True)
    status = models.TextField(default="Received")

    class Meta:
        db_table = "jobwork_inwards"
        ordering = ["-date", "-created_at"]


class JWOCharge(TenantModel):
    job_work_order = models.ForeignKey(
        JobWorkOrder, on_delete=models.CASCADE, related_name="charges"
    )
    type = models.TextField(default="")
    description = models.TextField(null=True, blank=True)
    rate = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    status = models.TextField(default="Active")

    class Meta:
        db_table = "jobwork_charges"
        ordering = ["created_at"]


class JWOReprocess(TenantModel):
    """Material received from a vendor and sent back for reprocessing.

    Lives nested under the Job Work Order like outwards/inwards. The
    received quantity is never stored here — the page resolves it live
    from the linked inward receipt (``inward_no``), so editing the
    receipt updates every reprocess row automatically.
    """

    STATUSES = [
        ("Pending", "Pending"),
        ("Sent to Vendor", "Sent to Vendor"),
        ("In Process", "In Process"),
        ("Received", "Received"),
        ("Completed", "Completed"),
        ("Cancelled", "Cancelled"),
    ]

    job_work_order = models.ForeignKey(
        JobWorkOrder, on_delete=models.CASCADE, related_name="reprocesses"
    )
    no = models.TextField(default="")
    inward_no = models.TextField(default="")
    date = models.DateField()
    qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    reason = models.TextField(default="")
    expected_return = models.DateField(null=True, blank=True)
    status = models.TextField(default="Pending")
    remarks = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "jobwork_reprocesses"
        ordering = ["-date", "-created_at"]


class VendorProcessInstruction(TenantModel, LegacyIdMixin):
    """Grey-fabric outside-processing instruction (PO → vendor).

    Frontend ERPContext shape kept 1:1 so the existing screens work
    unchanged: pi_number, po/vendor/fabric references as text plus
    quantities and dates. The server owns pi_number (VPI series).
    """

    STATUSES = [
        ("Pending", "Pending"),
        ("Draft", "Draft"),
        ("In Progress", "In Progress"),
        ("Running", "Running"),
        ("Partially Completed", "Partially Completed"),
        ("Completed", "Completed"),
        ("Cancelled", "Cancelled"),
    ]

    pi_number = models.TextField(db_index=True)
    po_id = models.TextField(null=True, blank=True)
    po_number = models.TextField(null=True, blank=True)
    vendor_id = models.TextField(null=True, blank=True)
    vendor = models.TextField(default="")
    fabric = models.TextField(default="")
    fabric_sku = models.TextField(null=True, blank=True)
    process_type = models.TextField(default="")
    assigned_employee = models.TextField(null=True, blank=True)
    assigned_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    produced_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    date = models.DateField()
    start_date = models.DateField(null=True, blank=True)
    expected_completion_date = models.DateField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="In Progress")
    remarks = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "jobwork_process_instructions"
        ordering = ["-date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "pi_number"], name="uq_jobwork_pi_number"
            )
        ]
        indexes = [
            models.Index(fields=["client", "-date"], name="ix_jobwork_pi_date"),
            models.Index(fields=["client", "status"], name="ix_jobwork_pi_status"),
        ]

    def __str__(self):
        return f"{self.pi_number} {self.process_type}"


class VPIProgressEntry(TenantModel):
    """Daily progress logged against a VendorProcessInstruction."""

    instruction = models.ForeignKey(
        VendorProcessInstruction, on_delete=models.CASCADE, related_name="entries"
    )
    date = models.DateField()
    produced_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    entered_by = models.TextField(null=True, blank=True)
    remarks = models.TextField(null=True, blank=True)
    #: Production proof photo (uploaded through /files/ first).
    photo_file = models.ForeignKey(
        "core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "jobwork_pi_entries"
        ordering = ["-date", "-created_at"]
