from django.db.models import Count, Q
from rest_framework import serializers

from apps.masters.serializers import PartySerializer
from apps.masters.views import PartyViewSet


class CustomerClientSerializer(PartySerializer):
    deals = serializers.IntegerField(source="deal_count", read_only=True, default=0)
    projects = serializers.IntegerField(source="project_count", read_only=True, default=0)

    class Meta(PartySerializer.Meta):
        fields = [*PartySerializer.Meta.fields, "deals", "projects"]

    def validate_type(self, value):
        if value not in ("Customer", "Both"):
            raise serializers.ValidationError("Clients must be customers or dual partners.")
        return value


class CustomerClientViewSet(PartyViewSet):
    serializer_class = CustomerClientSerializer
    permission_map = {"read": ["menu_admin"], "write": ["manage_company_profile"]}
    ordering = ["name", "id"]

    def get_queryset(self):
        client_id = self.get_client_id()
        return super().get_queryset().filter(type__in=["Customer", "Both"]).annotate(
            deal_count=Count(
                "deals",
                filter=Q(deals__deleted_at__isnull=True, deals__client_id=client_id),
                distinct=True,
            ),
            project_count=Count(
                "crm_projects",
                filter=Q(crm_projects__deleted_at__isnull=True, crm_projects__client_id=client_id),
                distinct=True,
            ) + Count(
                "pms_projects",
                filter=Q(pms_projects__deleted_at__isnull=True, pms_projects__client_id=client_id),
                distinct=True,
            ),
        )

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(status="Active")),
            withDeals=Count("id", filter=Q(deal_count__gt=0)),
            withProjects=Count("id", filter=Q(project_count__gt=0)),
        )
