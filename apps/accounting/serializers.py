"""Accounts serializers (api.md §8)."""
from rest_framework import serializers

from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    MoneyField,
    TenantPrimaryKeyRelatedField,
)

from .models import (
    Account, BankAccount, BankTransfer, Budget, ExpenseCategory, JournalEntry,
    JournalLine, StandaloneNote,
)


class AccountSerializer(BaseModelSerializer):
    class Meta:
        model = Account
        fields = [
            "id", "code", "name", "type", "subtype", "parent", "is_system",
            "system_key", "is_active", "opening_balance", "created_at", "updated_at",
        ]
        read_only_fields = ["is_system", "system_key", "created_at", "updated_at"]


class BankAccountSerializer(BaseModelSerializer):
    accountId = TenantPrimaryKeyRelatedField(source="account", queryset=Account.objects.all())
    accountName = serializers.CharField(source="account.name", read_only=True)
    balance = serializers.SerializerMethodField()

    class Meta:
        model = BankAccount
        fields = [
            "id", "name", "type", "accountId", "accountName", "account_number",
            "ifsc", "bank_name", "branch", "opening_balance", "balance",
            "is_default", "is_active", "created_at", "updated_at",
        ]

    def get_balance(self, bank_account):
        return getattr(bank_account, "computed_balance", None)


class JournalLineSerializer(BaseModelSerializer):
    accountId = TenantPrimaryKeyRelatedField(source="account", queryset=Account.objects.all())
    accountName = serializers.CharField(source="account.name", read_only=True)
    accountCode = serializers.CharField(source="account.code", read_only=True)
    partyId = TenantPrimaryKeyRelatedField(
        source="party", model="masters.Party", required=False, allow_null=True
    )
    partyName = serializers.CharField(source="party.name", read_only=True)

    class Meta:
        model = JournalLine
        fields = [
            "id", "accountId", "accountName", "accountCode", "partyId", "partyName",
            "debit", "credit", "line_no", "description",
        ]


class JournalEntrySerializer(BaseModelSerializer):
    """A manual entry must balance Dr = Cr (api.md §8).

    System entries are immutable and reversible only through the parent
    document's cancel (db.md §8), which the viewset enforces.
    """

    lines = JournalLineSerializer(many=True)
    sourceDocumentId = serializers.CharField(source="source_document_id", read_only=True)

    class Meta:
        model = JournalEntry
        fields = [
            "id", "entry_number", "entry_date", "narration", "status",
            "source_document_type", "sourceDocumentId", "is_system",
            "reversal_of", "posted_at", "lines", "created_at", "updated_at",
        ]
        read_only_fields = [
            "entry_number", "is_system", "reversal_of", "posted_at",
            "source_document_type", "created_at", "updated_at",
        ]

    def validate(self, attrs):
        lines = attrs.get("lines") or []
        if len(lines) < 2:
            raise serializers.ValidationError(
                {"lines": ["A journal entry needs at least two lines."]}
            )
        total_debit = sum((line.get("debit") or 0) for line in lines)
        total_credit = sum((line.get("credit") or 0) for line in lines)
        if total_debit != total_credit:
            raise serializers.ValidationError(
                {
                    "lines": [
                        f"Entry does not balance: debits {total_debit} vs credits {total_credit}."
                    ]
                }
            )
        return attrs


class BankTransferSerializer(BaseModelSerializer):
    fromAccountId = TenantPrimaryKeyRelatedField(
        source="from_account", queryset=BankAccount.objects.all()
    )
    toAccountId = TenantPrimaryKeyRelatedField(
        source="to_account", queryset=BankAccount.objects.all()
    )
    date = serializers.DateField(source="transfer_date")

    class Meta:
        model = BankTransfer
        fields = [
            "id", "fromAccountId", "toAccountId", "amount", "date", "reference",
            "notes", "journal_entry", "created_at",
        ]
        read_only_fields = ["journal_entry", "created_at"]

    def validate(self, attrs):
        source = attrs.get("from_account")
        destination = attrs.get("to_account")
        if source and destination and source.id == destination.id:
            raise serializers.ValidationError(
                {"toAccountId": ["Must differ from the source account."]}
            )
        return attrs


class BudgetSerializer(BaseModelSerializer):
    accountId = TenantPrimaryKeyRelatedField(source="account", queryset=Account.objects.all())
    accountName = serializers.CharField(source="account.name", read_only=True)
    actual = serializers.SerializerMethodField()
    variance = serializers.SerializerMethodField()

    class Meta:
        model = Budget
        fields = [
            "id", "accountId", "accountName", "period_start", "period_end",
            "amount", "actual", "variance", "notes", "created_at", "updated_at",
        ]

    def get_actual(self, budget):
        return getattr(budget, "actual_amount", None)

    def get_variance(self, budget):
        actual = getattr(budget, "actual_amount", None)
        if actual is None:
            return None
        return budget.amount - actual


class ExpenseCategorySerializer(BaseModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ["id", "name", "account", "is_active", "created_at"]


class LedgerEntrySerializer(BaseSerializer):
    date = serializers.DateField()
    documentType = serializers.CharField(allow_null=True)
    documentNumber = serializers.CharField(allow_null=True)
    description = serializers.CharField(allow_null=True)
    debit = MoneyField()
    credit = MoneyField()
    balance = MoneyField()


class StandaloneNoteSerializer(BaseModelSerializer):
    partyId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    partyName = serializers.CharField(source="party.name", read_only=True)
    returnReasonId = TenantPrimaryKeyRelatedField(
        source="return_reason", model="masters.ReturnReasonMaster", required=False, allow_null=True
    )
    returnReasonName = serializers.CharField(source="return_reason.name", read_only=True)

    class Meta:
        model = StandaloneNote
        fields = [
            "id", "note_type", "note_number", "note_date", "partyId", "partyName",
            "against_document_type", "against_document_id", "against_document_number",
            "returnReasonId", "returnReasonName", "reason_notes", "taxable_amount",
            "cgst", "sgst", "igst", "total_amount", "status", "approved_by",
            "approved_at", "journal_entry", "notes", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "note_number", "total_amount", "status", "approved_by",
            "approved_at", "journal_entry", "created_at", "updated_at",
        ]
