from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.crm.views import MasterTaskViewSet


class MasterTaskDeletionTest(SimpleTestCase):
    def test_master_task_delete_remains_available_to_authorized_users(self):
        task_id = uuid4()
        view = MasterTaskViewSet.as_view({
            "get": "retrieve", "put": "update",
            "patch": "partial_update", "delete": "destroy",
        })
        for is_superuser in (False, True):
            with self.subTest(is_superuser=is_superuser):
                request = APIRequestFactory().delete(f"/crm/master-tasks/{task_id}/")
                request.client_id = uuid4()
                force_authenticate(request, user=SimpleNamespace(
                    is_authenticated=True, is_superuser=is_superuser,
                    status="Active", permission_ids=["manage_pipeline"],
                ))
                with (
                    patch.object(MasterTaskViewSet, "get_object") as get_object,
                    patch.object(MasterTaskViewSet, "perform_destroy") as perform_destroy,
                ):
                    response = view(request, pk=task_id)

                self.assertEqual(response.status_code, 204)
                self.assertIn("DELETE", response["Allow"])
                self.assertIn("PATCH", response["Allow"])
                get_object.assert_called_once()
                perform_destroy.assert_called_once_with(get_object.return_value)
