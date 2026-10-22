file_path = r'C:\Users\evenmore\oscar\oscar-backend\apps\pms\production_serializers.py'
with open(file_path, 'r', encoding='utf-8') as f:
    text = f.read()

target = """    employeeCode = serializers.ReadOnlyField(source="employee.employee_code")
    employeeName = serializers.ReadOnlyField(source="employee.name")
    cumulative = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()
    totalProduced = serializers.SerializerMethodField()
    acceptedQty = serializers.SerializerMethodField()
    shortageQty = serializers.SerializerMethodField()
    finalAmount = serializers.SerializerMethodField()

    class Meta:
        model = ProductionInstruction
        fields = [
            "id", "instruction_number", "agency_name", "agencyId", "agencyCode", "order_reference", "order_meter",
            "taka_count", "total_weight", "width", "gsm", "tentative_rate", "agreed_rate", "rate_uom",
            "special_terms", "expected_start_date", "pi_date", "fabric", "process_type", "agreed_job_rate", "rejected_qty",
            "verification_remarks", "completed_at", "greyLotId", "greyLotNo", "finalPoId", "finalPoNumber",
            "employeeId", "employeeCode", "employeeName", "supervisorId",
            "status", "verified_by", "verified_at", "notes",
            "cumulative", "balance", "totalProduced", "acceptedQty",
            "shortageQty", "finalAmount", "created_at", "updated_at",
        ]"""

replacement = """    employeeCode = serializers.ReadOnlyField(source="employee.employee_code")
    employeeName = serializers.ReadOnlyField(source="employee.name")
    supervisorName = serializers.ReadOnlyField(source="supervisor.name")
    expectedStartDate = serializers.DateField(
        source="expected_start_date", required=False, allow_null=True
    )
    expectedCompletionDate = serializers.DateField(
        source="expected_completion_date", required=False, allow_null=True
    )
    cumulative = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()
    totalProduced = serializers.SerializerMethodField()
    acceptedQty = serializers.SerializerMethodField()
    shortageQty = serializers.SerializerMethodField()
    finalAmount = serializers.SerializerMethodField()

    class Meta:
        model = ProductionInstruction
        fields = [
            "id", "instruction_number", "agency_name", "agencyId", "agencyCode", "order_reference", "order_meter",
            "taka_count", "total_weight", "width", "gsm", "tentative_rate", "agreed_rate", "rate_uom",
            "special_terms", "expected_start_date", "expectedStartDate", "expected_completion_date", "expectedCompletionDate",
            "pi_date", "fabric", "process_type", "agreed_job_rate", "rejected_qty",
            "verification_remarks", "completed_at", "greyLotId", "greyLotNo", "finalPoId", "finalPoNumber",
            "employeeId", "employeeCode", "employeeName", "supervisorId", "supervisorName",
            "status", "verified_by", "verified_at", "notes",
            "cumulative", "balance", "totalProduced", "acceptedQty",
            "shortageQty", "finalAmount", "created_at", "updated_at",
        ]"""

norm_text = text.replace('\r\n', '\n')
norm_target = target.replace('\r\n', '\n')
if norm_target in norm_text:
    new_text = norm_text.replace(norm_target, replacement)
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_text)
    print("SUCCESS: production_serializers.py updated!")
else:
    print("Target not found!")
